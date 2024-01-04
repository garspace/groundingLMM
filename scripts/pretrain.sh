MASTER_PORT=29005
PRETRAINED_HF_PATH=GLaMM-GranD-Pretrained
GROUNDING_ENC_CKPT_PATH=sam_vit_huge/sam_vit_h_4b8939.pth
OUTPUT_DIR_PATH=glmm

# reg
#deepspeed --include localhost:4,5 --master_port $MASTER_PORT train.py \
#--version $PRETRAINED_HF_PATH \
#--dataset_dir ./data/ \
#--vision_pretrained $GROUNDING_ENC_CKPT_PATH \
#--exp_name $OUTPUT_DIR_PATH \
#--lora_r 8 \
#--lr 3e-4 \
#--pretrained --use_reg_data \
#--reg_dataset 'RefCocoG_Reg' \
#--reg_sample_rates "1" \
#--val_dataset 'RefCOCOgRegVal' \
#--epochs 5 \
#--steps_per_epoch 500

# seg   --pretrained
deepspeed --include localhost:4,5 --master_port $MASTER_PORT train.py \
--version $PRETRAINED_HF_PATH \
--dataset_dir ./data/ \
--vision_pretrained $GROUNDING_ENC_CKPT_PATH \
--exp_name $OUTPUT_DIR_PATH \
--lora_r 8 \
--lr 3e-4 \
--use_segm_data \
--seg_dataset "Refer_Segm" \
--segm_sample_rates "1" \
--refer_segm_data "refcoco||refcoco+||refcocog" \
--val_dataset "RefCOCOgSegVal" \
--epochs 5 \
--steps_per_epoch 350 \
--mask_validation 
