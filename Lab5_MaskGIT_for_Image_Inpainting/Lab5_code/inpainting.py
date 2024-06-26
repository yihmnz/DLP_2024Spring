import pandas as pd
import numpy as np
from PIL import Image
import torch
from torchvision import transforms
import argparse
from utils import LoadTestData, LoadMaskData
from torch.utils.data import Dataset,DataLoader
from torchvision import utils as vutils
import os
from models import MaskGit as VQGANTransformer
import yaml
import torch.nn.functional as F
import math
from tqdm import tqdm

class MaskGIT:
    def __init__(self, args, MaskGit_CONFIGS):
        self.device=args.device
        self.model = VQGANTransformer(MaskGit_CONFIGS["model_param"]).to(device=args.device)
        self.model.load_transformer_checkpoint(args.load_transformer_ckpt_path, self.device)
        self.model.eval()
        self.total_iter=args.total_iter
        self.mask_func=args.mask_func
        self.sweet_spot=args.sweet_spot
        self.prepare()

    @staticmethod
    def prepare():
        os.makedirs(args.save_root, exist_ok=True)
        # os.makedirs(os.path.join(args.save_root,"test_results"), exist_ok=True)
        os.makedirs(os.path.join(args.save_root,"mask_scheduling"), exist_ok=True)
        os.makedirs(os.path.join(args.save_root,"imga"), exist_ok=True)

    def update_ratio(self, step, method='linear', init_ratio = 1):
        if method == 'linear':
            return init_ratio*(1- (step / self.total_iter))
        elif method == 'cosine':
            return init_ratio*(0.5 * (1 + math.cos(math.pi * step / self.total_iter)))
        elif method == 'square':
            return init_ratio*(1 - (step / self.total_iter) ** 2)
        else:
            raise ValueError("Unsupported scheduling method")
        
##TODO3 step1-1: total iteration decoding  
#mask_b: iteration decoding initial mask, where mask_b is true means mask
    def inpainting(self,image,mask_b,i): #MakGIT inference
        maska = torch.zeros(self.total_iter, 3, 16, 16) #save all iterations of masks in latent domain
        imga = torch.zeros(self.total_iter+1, 3, 64, 64)#save all iterations of decoded images
        mean = torch.tensor([0.4868, 0.4341, 0.3844],device=self.device).view(3, 1, 1)  
        std = torch.tensor([0.2620, 0.2527, 0.2543],device=self.device).view(3, 1, 1)
        ori=(image[0]*std)+mean
        imga[0]=ori #mask the first image be the ground truth of masked image

        self.model.eval()
        with torch.no_grad():
            mask_bc=mask_b
            mask_b=mask_b.to(device=self.device)
            mask_bc=mask_bc.to(device=self.device)
            true_count = mask_bc.sum().item()
            true_count1 = true_count
            mask_diff_count = 0
            total_count = mask_bc.numel()
            init_ratio = true_count / total_count
            ratio = 0
            last_epoch = False
            token_update = torch.zeros(1, 256).to(device=self.device)
            
            #iterative decoding for loop design
            #Hint: it's better to save original mask and the updated mask by scheduling separately
            for step in range(self.total_iter):
                if step == self.sweet_spot:
                    break
                elif step == self.total_iter-1:
                    last_epoch = True
                    # print('true')
                ratio = self.update_ratio(step+1, method=self.mask_func, init_ratio = 1)
                
                z_indices_predict, mask_bc = self.model.inpainting(image, mask_b, token_update, ratio, true_count, mask_diff_count, last_epoch = last_epoch)
                # Store updated tokens
                unique_mask = mask_b ^ mask_bc
                token_update += z_indices_predict * unique_mask
                
                #static method yon can modify or not, make sure your visualization results are correct
                mask_i=mask_bc.view(1, 16, 16)
                mask_image = torch.ones(3, 16, 16)
                indices = torch.nonzero(mask_i, as_tuple=False)#label mask true
                mask_image[:, indices[:, 1], indices[:, 2]] = 0 #3,16,16
                maska[step]=mask_image

                #update mask
                mask_b = mask_bc
                # correct mask_diff for iterative decoding
                mask_diff_count = true_count1-mask_bc.sum().item()
                # Decode the predicted indices to image
                shape=(1,16,16,256)
                num_embeddings = self.model.vqgan.codebook.embedding.num_embeddings
                if (z_indices_predict >= num_embeddings).any():
                    out_of_bounds_mask = (z_indices_predict >= num_embeddings)
                    z_indices_predict[out_of_bounds_mask] = num_embeddings-1
                    print('Modified')
                z_q = self.model.vqgan.codebook.embedding(z_indices_predict).view(shape)
                z_q = z_q.permute(0, 3, 1, 2)
                decoded_img=self.model.vqgan.decode(z_q)
                dec_img_ori=(decoded_img[0]*std)+mean
                imga[step+1]=dec_img_ori #get decoded image
 
            ##decoded image of the sweet spot only, the test_results folder path will be the --predicted-path for fid score calculation
                if step >= self.sweet_spot/self.sweet_spot:
                    os.makedirs(os.path.join(args.save_root,"test_results"+str(step)), exist_ok=True)
                    vutils.save_image(dec_img_ori, os.path.join(args.save_root,"test_results"+str(step), f"image_{i:03d}.png"), nrow=1) 
            
            #demo score 
            vutils.save_image(maska, os.path.join(args.save_root, "mask_scheduling", f"test_{i}.png"), nrow=10) 
            vutils.save_image(imga, os.path.join(args.save_root, "imga", f"test_{i}.png"), nrow=7)



class MaskedImage:
    def __init__(self, args):
        mi_ori=LoadTestData(root= args.test_maskedimage_path, partial=args.partial)
        self.mi_ori =  DataLoader(mi_ori,
                            batch_size=args.batch_size,
                            num_workers=args.num_workers,
                            drop_last=True,
                            pin_memory=True,
                            shuffle=False)
        mask_ori =LoadMaskData(root= args.test_mask_path, partial=args.partial)
        self.mask_ori =  DataLoader(mask_ori,
                            batch_size=args.batch_size,
                            num_workers=args.num_workers,
                            drop_last=True,
                            pin_memory=True,
                            shuffle=False)
        self.device=args.device

    def get_mask_latent(self,mask):    
        downsampled1 = torch.nn.functional.avg_pool2d(mask, kernel_size=2, stride=2)
        resized_mask = torch.nn.functional.avg_pool2d(downsampled1, kernel_size=2, stride=2)
        resized_mask[resized_mask != 1] = 0       #1,3,16*16   check use  
        mask_tokens=(resized_mask[0][0]//1).flatten()   ##[256] =16*16 token
        mask_tokens=mask_tokens.unsqueeze(0)
        mask_b = torch.zeros(mask_tokens.shape, dtype=torch.bool, device=self.device)
        mask_b |= (mask_tokens == 0) #true means mask
        return mask_b


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="MaskGIT for Inpainting")
    parser.add_argument('--device', type=str, default="cuda", help='Which device the training is on.')#cuda
    parser.add_argument('--batch-size', type=int, default=1, help='Batch size for testing.')
    parser.add_argument('--partial', type=float, default=1.0, help='Number of epochs to train (default: 50)')    
    parser.add_argument('--num_workers', type=int, default=4, help='Number of worker')
    
    parser.add_argument('--MaskGitConfig', type=str, default='./Lab5_code/config/MaskGit.yml', help='Configurations for MaskGIT')
    
    
#TODO3 step1-2: modify the path, MVTM parameters
    parser.add_argument('--load_transformer_ckpt_path', type=str, default='', help='load ckpt')
    #dataset path
    parser.add_argument('--test-maskedimage-path', type=str, default='./lab5_dataset/cat_face/masked_image', help='Path to testing image dataset.')
    parser.add_argument('--test-mask-path', type=str, default='./lab5_dataset/mask64', help='Path to testing mask dataset.')
    #MVTM parameter
    parser.add_argument('--sweet-spot', type=int, default=15, help='sweet spot: the best step in total iteration')
    parser.add_argument('--total-iter', type=int, default=30, help='total step for mask scheduling')
    parser.add_argument('--mask-func', type=str, default='0', help='mask scheduling function')
    parser.add_argument('--save_root',     type=str, required=True,  help="The path to save your data")
    
    args = parser.parse_args()

    t=MaskedImage(args)
    MaskGit_CONFIGS = yaml.safe_load(open(args.MaskGitConfig, 'r'))
    maskgit = MaskGIT(args, MaskGit_CONFIGS)

    for i, (image, mask) in enumerate(tqdm(zip(t.mi_ori, t.mask_ori), total=len(t.mi_ori))):
        image = image.to(device=args.device)
        mask = mask.to(device=args.device)
        mask_b = t.get_mask_latent(mask)
        maskgit.inpainting(image, mask_b, i)
    # i=0
    # for image, mask in tqdm(zip(t.mi_ori, t.mask_ori)):
    #     image=image.to(device=args.device)
    #     mask=mask.to(device=args.device)
    #     mask_b=t.get_mask_latent(mask)       
    #     maskgit.inpainting(image,mask_b,i)
    #     i+=1