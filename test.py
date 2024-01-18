import cv2,random
import numpy as np
import torch.nn.functional as F
from PIL import Image
from model.llava.mm_utils import tokenizer_image_token
import transformers,torch,sys,argparse,os,collections
from transformers import LlamaConfig, LlamaForCausalLM, LlamaTokenizer
from model.GLaMM import GLaMMForCausalLM
from transformers import AutoConfig, AutoModelForCausalLM, AutoTokenizer, CLIPImageProcessor
from model.SAM.utils.transforms import ResizeLongestSide
from model.llava import conversation as conversation_lib
from utils.utils import DEFAULT_IM_END_TOKEN, DEFAULT_IM_START_TOKEN, DEFAULT_IMAGE_TOKEN, IMAGE_TOKEN_INDEX
from utils.markdown_utils import markdown_default, examples, title, description, article, process_markdown, colors, draw_bbox


def parse_args(args):
    parser = argparse.ArgumentParser(description="GLaMM Model Training")

    # Model-specific settings
    parser.add_argument("--version", default="./hf_weight")
    #parser.add_argument("--version", default="./output/refsegm_finetune")
    parser.add_argument("--vision-tower", default="./pretrained_ckpt/clip-vit-large-patch14-336", type=str)
    parser.add_argument("--vis_save_path", default="./vis_output", type=str)
    parser.add_argument("--precision", default='bf16', type=str)
    parser.add_argument("--image_size", default=1024, type=int, help="Image size for grounding image encoder")
    parser.add_argument("--model_max_length", default=1536, type=int)
    parser.add_argument("--lora_r", default=8, type=int)
    parser.add_argument("--local-rank", default=0, type=int, help="node rank")
    parser.add_argument("--use_mm_start_end", action="store_true", default=True)
    parser.add_argument("--conv_type", default="llava_v1", type=str, choices=["llava_v1", "llava_llama_2"])

    return parser.parse_args(args)

def grounding_enc_processor(x: torch.Tensor) -> torch.Tensor:
    IMG_MEAN = torch.Tensor([123.675, 116.28, 103.53]).view(-1, 1, 1)
    IMG_STD = torch.Tensor([58.395, 57.12, 57.375]).view(-1, 1, 1)
    IMG_SIZE = 1024
    x = (x - IMG_MEAN) / IMG_STD
    h, w = x.shape[-2:]
    x = F.pad(x, (0, IMG_SIZE - w, 0, IMG_SIZE - h))
    return x


def region_enc_processor(orig_size, post_size, bbox_img):
    orig_h, orig_w = orig_size
    post_h, post_w = post_size
    y_scale = post_h / orig_h
    x_scale = post_w / orig_w

    bboxes_scaled = [[bbox[0] * x_scale, bbox[1] * y_scale, bbox[2] * x_scale, bbox[3] * y_scale] for bbox in bbox_img]

    tensor_list = []
    for box_element in bboxes_scaled:
        ori_bboxes = np.array([box_element], dtype=np.float64)
        # Normalizing the bounding boxes
        norm_bboxes = ori_bboxes / np.array([post_w, post_h, post_w, post_h])
        # Converting to tensor, handling device and data type as in the original code
        tensor_list.append(torch.tensor(norm_bboxes, device='cuda').half().to(torch.bfloat16))

    if len(tensor_list) > 1:
        bboxes = torch.stack(tensor_list, dim=1)
        bboxes = [bboxes.squeeze()]
    else:
        bboxes = tensor_list
    return bboxes

def prepare_mask(input_image, image_np, pred_masks, text_output, color_history):
    save_img = None
    for i, pred_mask in enumerate(pred_masks):
        if pred_mask.shape[0] == 0:
            continue
        pred_mask = pred_mask.detach().cpu().numpy()
        mask_list = [pred_mask[i] for i in range(pred_mask.shape[0])]
        if len(mask_list) > 0:
            save_img = image_np.copy()
            colors_temp = colors
            seg_count = text_output.count("[SEG]")
            mask_list = mask_list[-seg_count:]
            for curr_mask in mask_list:
                color = random.choice(colors_temp)
                if len(colors_temp) > 0:
                    colors_temp.remove(color)
                else:
                    colors_temp = colors
                color_history.append(color)
                msk= curr_mask.copy()
                curr_mask = curr_mask > 0
                save_img[curr_mask] = (image_np * 0.5 + curr_mask[:, :, None].astype(np.uint8) * np.array(color) * 0.5)[
                    curr_mask]
    seg_mask = np.zeros((curr_mask.shape[0], curr_mask.shape[1], 3), dtype=np.uint8)
    seg_mask[curr_mask] = [255, 255, 255]  # white for True values
    seg_mask[~curr_mask] = [0, 0, 0]  # black for False values
    seg_mask = Image.fromarray(seg_mask)
    mask_path = input_image.replace('image', 'mask')
    seg_mask.save(mask_path)
    return save_img

def inference(input_str, all_inputs, follow_up, generate):
    bbox_img = all_inputs['boxes']
    input_image = all_inputs['image']

    print("input_str: ", input_str, "input_image: ", input_image)

    if generate:
        return generate_new_image(st_pipe, input_str, input_image)

    if not follow_up:
        conv = conversation_lib.conv_templates[args.conv_type].copy()
        conv.messages = []
        conv_history = {'user': [], 'model': []}
        conv_history["user"].append(input_str)

    input_str = input_str.replace('&lt;', '<').replace('&gt;', '>')
    prompt = input_str
    prompt = f"The {DEFAULT_IMAGE_TOKEN} provides an overview of the picture." + "\n" + prompt
    if args.use_mm_start_end:
        replace_token = (DEFAULT_IM_START_TOKEN + DEFAULT_IMAGE_TOKEN + DEFAULT_IM_END_TOKEN)
        prompt = prompt.replace(DEFAULT_IMAGE_TOKEN, replace_token)

    if not follow_up:
        conv.append_message(conv.roles[0], prompt)
        conv.append_message(conv.roles[1], "")
    else:
        conv.append_message(conv.roles[0], input_str)
        conv.append_message(conv.roles[1], "")
    prompt = conv.get_prompt()

    image_np = cv2.imread(input_image)
    image_np = cv2.cvtColor(image_np, cv2.COLOR_BGR2RGB)
    orig_h, orig_w = image_np.shape[:2]
    original_size_list = [image_np.shape[:2]]

    # Prepare input for Global Image Encoder
    global_enc_image = global_enc_processor.preprocess(
        image_np, return_tensors="pt")["pixel_values"][0].unsqueeze(0).cuda()
    global_enc_image = global_enc_image.bfloat16()

    # Prepare input for Grounding Image Encoder
    image = transform.apply_image(image_np)
    resize_list = [image.shape[:2]]
    grounding_enc_image = (grounding_enc_processor(torch.from_numpy(image).permute(2, 0, 1).
                                                   contiguous()).unsqueeze(0).cuda())
    grounding_enc_image = grounding_enc_image.bfloat16()

    # Prepare input for Region Image Encoder
    post_h, post_w = global_enc_image.shape[1:3]
    bboxes = None
    if bbox_img is not None and len(bbox_img) > 0:
        bboxes = region_enc_processor((orig_h, orig_w), (post_h, post_w), bbox_img)

    input_ids = tokenizer_image_token(prompt, tokenizer, return_tensors="pt")
    input_ids = input_ids.unsqueeze(0).cuda()

    # Pass prepared inputs to model
    output_ids, pred_masks = model.evaluate(
        global_enc_image, grounding_enc_image, input_ids, resize_list, original_size_list, max_tokens_new=512,
        bboxes=bboxes)
    output_ids = output_ids[0][output_ids[0] != IMAGE_TOKEN_INDEX]

    text_output = tokenizer.decode(output_ids, skip_special_tokens=False)
    text_output = text_output.replace("\n", "").replace("  ", " ")
    text_output = text_output.split("ASSISTANT: ")[-1]
    print("text_output: ", text_output)

    # For multi-turn conversation
    conv.messages.pop()
    conv.append_message(conv.roles[1], text_output)
    conv_history["model"].append(text_output)
    color_history = []
    save_img = None
    if "[SEG]" in text_output:
        save_img = prepare_mask(input_image, image_np, pred_masks, text_output, color_history)

    output_str = text_output  # input_str
    if save_img is not None:
        output_image = save_img  # input_image
    else:
        if bbox_img is not None and len(bbox_img) > 0:
            output_image = draw_bbox(image_np.copy(), bbox_img)
        else:
            output_image = input_image

    markdown_out = process_markdown(output_str, color_history)

    return output_image, markdown_out


args = parse_args(sys.argv[1:])

tokenizer = transformers.AutoTokenizer.from_pretrained(
        "GLaMM-GranD-Pretrained", model_max_length=1536, padding_side="right", use_fast=False
    )
tokenizer.pad_token = tokenizer.unk_token

args.bbox_token_idx = tokenizer("<bbox>", add_special_tokens=False).input_ids[0]
args.seg_token_idx = tokenizer("[SEG]", add_special_tokens=False).input_ids[0]
args.bop_token_idx = tokenizer("<p>", add_special_tokens=False).input_ids[0]
args.eop_token_idx = tokenizer("</p>", add_special_tokens=False).input_ids[0]

model_args = {k: getattr(args, k) for k in ["seg_token_idx", "bbox_token_idx", "eop_token_idx", "bop_token_idx"]}

model = GLaMMForCausalLM.from_pretrained(args.version, torch_dtype=torch.bfloat16, low_cpu_mem_usage=True, **model_args)
model.get_model().initialize_vision_modules(model.get_model().config)
vision_tower = model.get_model().get_vision_tower()
vision_tower.to(dtype=torch.bfloat16, device=args.local_rank)

model = model.bfloat16().cuda()
global_enc_processor = CLIPImageProcessor.from_pretrained(model.config.vision_tower)
transform = ResizeLongestSide(args.image_size)
model.eval()

text_input = 'Please describe in detail the contents of the image. Please output segmentation mask.'
#inputs_dict = {'boxes': [], 'image': './test_data/COCO_train2014_000000000110_image.jpg'}
inputs_dict = {'boxes': None, 'image': './test_data/COCO_train2014_000000000110_image.jpg'}
output_image, markdown_output = inference(text_input, inputs_dict, follow_up=False, generate=False)
cv2.imwrite('out.jpg',output_image[:,:,::-1])
