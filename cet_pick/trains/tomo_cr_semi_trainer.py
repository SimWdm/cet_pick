from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import torch
import numpy as np

from cet_pick.models.loss import FocalLoss, RegL1Loss, RegLoss, UnbiasedConLoss, ConsistencyLoss, PULoss, BiasedConLoss, SupConLossV2_more, PUGELoss
from cet_pick.models.decode import _nms
from cet_pick.models.utils import _sigmoid 
from cet_pick.trains.base_trainer import BaseTrainer 
from cet_pick.utils.debugger import Debugger
from cet_pick.utils.post_process import tomo_post_process
import cv2
from cet_pick.models.decode import tomo_decode

from pytorch_metric_learning import miners, losses

class TomoCRSemiLoss(torch.nn.Module):
    """
    Trainer for PU Learner with Contrastive Regularization
    
    """
    def __init__(self, opt):
        super(TomoCRSemiLoss, self).__init__()
        # self.crit = torch.nn.MSELoss() if opt.mse_loss else FocalLoss()
        # self.crit = torch.nn.MSELoss() if opt.mse_loss else PULoss(opt.tau)
        # self.crit2 = FocalLoss()
        # self.crit = PULoss(opt.tau)
        # # self.crit2 = torch.nn.MSELoss() if opt.mse_loss else FocalLoss()

        # self.cr_loss = UnbiasedConLoss(opt.temp, opt.tau)
        if opt.pn:
            # print('using pn loss')
            self.crit = FocalLoss()
        elif opt.ge:
            criteria = FocalLoss()
            self.crit = PUGELoss(opt.tau, criteria=criteria)
        else:
            self.crit = PULoss(opt.tau)
        self.crit2 = FocalLoss()
        
        # self.crit2 = torch.nn.MSELoss() if opt.mse_loss else FocalLoss()
        if opt.pn:
            # print('using pn cr loss')
            self.cr_loss = SupConLossV2_more(opt.temp)
        else:
            self.cr_loss = UnbiasedConLoss(opt.temp, opt.tau)
        # self.cr_loss = SupConLossV2_more(0.07,0.07,0.07)
        # self.cr_loss = losses.TripletMarginLoss()
        # self.cr_loss = BiasedConLoss(opt.temp)
        # self.unsup_cr_loss = UnSupConLoss(0.07)
        self.cons_loss = ConsistencyLoss()
        
        self.opt = opt 

    def forward(self, outputs, batch, epoch, phase, output_cr = None):

        opt = self.opt 

        cr_loss, hm_loss, consis_loss = 0, 0, 0
        for s in range(opt.num_stacks):
            output = outputs[s]
            if output_cr is not None:
                output_cr = output_cr[s]
            if 1:
                output['hm'] = _sigmoid(output['hm'])
                if output_cr is not None:
                    output_cr['hm'] = _sigmoid(output_cr['hm'])
        #         out_mask = _sigmoid(7*(output['hm']-0.5))
        # print('outuput hm', output['hm'].shape)
        if phase == 'train':
            hm_loss += self.crit(output['hm'], batch['hm']) / opt.num_stacks 
        else:
            hm_loss += self.crit2(output['hm'], batch['hm']) / opt.num_stacks
        # hm_loss += self.crit(output_cr['hm'], batch['hm_c']) / opt.num_stacks
        if opt.contrastive and phase == "train":
            # hm_loss += self.crit(output_cr['hm'], batch['hm_c']) / opt.num_stacks
            output_fm = output['proj']
            b, ch, d, w, h = output_fm.shape
            # print('outputfm', output_fm.shape)
            gt_hm = batch['hm']
            # print('gt_hm', gt_hm.shape)
            output_fm_cr = output_cr['proj']
            flip_prob = batch['flip_prob']
            if flip_prob > 0.5:
                output_fm_cr = output_fm_cr.flip(-2)
                output_cr = output_cr['hm'].flip(-2)
            else:
                output_fm_cr = output_fm_cr.flip(-1)
                output_cr = output_cr['hm'].flip(-1)
            output_fm = output_fm.reshape(b, ch, -1).contiguous()
            output_fm = output_fm.permute(1,0,2)
            output_fm = output_fm.reshape(ch, -1).T
            # output_fm = output_fm.reshape((1, 16, -1))
            # output_fm = output_fm.squeeze().T 
            # print('output_fm', output_fm.shape)
            # output_fm_cr = output_fm_cr.reshape((1, 16, -1))
            # output_fm_cr = output_fm_cr.squeeze().T
            output_fm_cr = output_fm_cr.reshape(b, ch, -1).contiguous()
            output_fm_cr = output_fm_cr.permute(1,0,2)
            output_fm_cr = output_fm_cr.reshape(ch, -1).T
            gt_hm_f = gt_hm.reshape(b, -1).contiguous()
            # gt_hm_f = gt_hm.reshape(1, -1)
            gt_hm_f = gt_hm_f.reshape(-1).contiguous()
            # print('gt_hm_f', gt_hm_f.shape)
            # print('output_hm', output['hm'].shape)
            output_hm = output['hm'].reshape(b, -1).contiguous()
            # output_hm = output_hm.squeeze()
            output_hm = output_hm.reshape(-1).contiguous()
            # print('reshaped', output_hm.shape)
            output_cr = output_cr.reshape(b, -1).contiguous()
            # output_cr = output_cr.squeeze()
            output_cr = output_cr.reshape(-1).contiguous()
            if self.opt.pn:
                loss_cr = self.cr_loss(gt_hm_f, output_hm, output_cr, output_fm, output_fm_cr, opt)
                cr_loss += loss_cr
            else:
                debiased_loss_sup, debiased_loss_unsup = self.cr_loss(gt_hm_f, output_hm, output_cr, output_fm, output_fm_cr, opt)
            # biased_loss_sup, biased_loss_unsup = self.cr_loss(gt_hm_f, output_fm, output_fm_cr, opt)
            
                cr_loss += debiased_loss_sup + 0.5*debiased_loss_unsup
            # debiased_loss_sup, debiased_loss_unsup = self.cr_loss(gt_hm_f, output_hm, output_cr, output_fm, output_fm_cr, opt)
            # biased_loss_sup, biased_loss_unsup = self.cr_loss(gt_hm_f, output_fm, output_fm_cr, opt)
            consis_loss += self.cons_loss(output_hm, output_cr)
            # cr_loss += debiased_loss_sup + 0.5*debiased_loss_unsup
            # cr_loss += 0.01*biased_loss_sup + biased_loss_unsup
            # gt_hm_cr = batch['hm_c']
            # output_fm_cr = output_cr['proj']
            # print('output_fm', output_fm.shape)
            # print('gt_hm', gt_hm.shape)
            # cr_loss += self.cr_loss(output_fm, gt_hm, opt)
            # cr_loss = hm_loss * 0
            # loss = cr_loss
            # loss =  hm_loss + cr_loss * 0.1
            # loss = hm_loss
            # if pretrain:
            #     loss = cr_loss
            # else:
            # cr_loss = hm_loss * 0
            # loss = hm_loss + 0.1 * consis_loss
            loss = hm_loss + cr_loss * self.opt.cr_weight + 0.1 * consis_loss
            # loss = hm_loss + cr_loss * self.opt.cr_weight
        else:
            cr_loss = hm_loss * 0
            loss = hm_loss
            consis_loss = hm_loss * 0


        loss_stats = {'loss': loss,'hm_loss': hm_loss, 'cr_loss': cr_loss, 'consis_loss': consis_loss}
        # loss_stats = {'loss': loss,'hm_loss': hm_loss, 'consis_loss': consis_loss}
        return loss, loss_stats

class TomoCRSemiTrainer(BaseTrainer):
    def __init__(self, opt, model, optimizer=None):
        super(TomoCRSemiTrainer, self).__init__(opt, model, optimizer=optimizer)

    def _get_losses(self, opt):
        loss_states = ['loss','hm_loss','cr_loss', 'consis_loss']
        # loss_states = ['loss','hm_loss', 'consis_loss']
        loss = TomoCRSemiLoss(opt)
        return loss_states, loss 

    def debug(self, batch, output, iter_id):
        opt = self.opt 
        # reg = output['reg'] if not opt.reg_offset else None 
        # print('output sim mat', output['sim_map'].shape)
        dets = tomo_decode(output['hm'].sigmoid_(), reg=None)
        dets = dets.detach().cpu().numpy().reshape(1, -1, dets.shape[2])
        if opt.task == 'semi3d':
            dets[:,:,:] *= opt.down_ratio
        else:
            dets[:,:,:2] *= opt.down_ratio 
        post_dets = tomo_post_process(dets, z_dim_tot = 128)
        dets_gt = batch['meta']['gt_det'].numpy().reshape(1, -1, 3)
        name = batch['meta']['name']
        if opt.task == 'semi3d':
            dets_gt[:,:,:] *= opt.down_ratio 
        else:

            dets_gt[:,:,:2] *= opt.down_ratio 
        post_dets_gt = tomo_post_process(dets_gt)
        # print('name', name)
        # print(dets_gt.shape)
        # print('post_dets', post_dets)
        # print('post_dets_gt', post_dets_gt)
        # print('tomo', batch['input'].shape)
        for i in range(1):
            debugger = Debugger(dataset = opt.dataset, down_ratio = opt.down_ratio)
            tomo = batch['input'][i].detach().cpu().numpy()
            gts = post_dets_gt[i]
            preds = post_dets[i]
            name = name[i]
            # print('name', name)
            # print('preds', preds)
            det_zs = preds.keys()
            debugger.save_detection(preds, path = opt.debug_dir, name=name)
            # print(det_zs)
            for z in np.arange(30,75):
                if opt.task == 'semi3d':
                    # z = int(z//2)
                    out_z = output['hm'][i].detach().cpu().numpy()[0][int(z//2)]
                    out_z_n = output['hm'][i]
                # print('out_proj_fist', output['proj'][i].shape)
                # out_z_sum = torch.sum(output['proj'][i], axis = 0)
                # out_z_sum = output['proj'][i][0]
                # print('out z sum shape', out_z_sum.shape)
                # out_proj = output['proj'][i].detach().cpu().numpy()[0][z]
                # out_proj = out_z_sum.detach().cpu().numpy()[z]
                # print('out_proj', out_proj.shape)
                # sim_mat_z = output['sim_map'].detach().cpu().numpy()[z]
                # print('out_z,', np.max(out_z))
                # sim_mat_z = cv2.normalize(sim_mat_z, None, alpha=0, beta=1, norm_type=cv2.NORM_MINMAX, dtype=cv2.CV_32F)
                # print('out_z_n', out_z_n.shape)
                    out_z_nms = _nms(out_z_n)
                # sim_mat_z_nms = _nms(output['sim_map'].unsqueeze(0))
                # print('sim_mat_z_nms', sim_mat_z_nms.shape)
                    out_z_nms = out_z_nms.detach().cpu().numpy()[0][int(z//2)]
                    out_z_gt = batch['hm'][i].detach().cpu().numpy()[int(z//2)]
                else:
                    out_z = output['hm'][i].detach().cpu().numpy()[0][z]
                    out_z_n = output['hm'][i]
                    out_z_nms = _nms(out_z_n)
                    out_z_nms = out_z_nms.detach().cpu().numpy()[0][z]
                    out_z_gt = batch['hm'][i].detach().cpu().numpy()[z]
                # print('out_z_gt', out_z_gt.shape)
                # out_z_n = out_z_n.detach().cpu().numpy()[0][z]
                out_z = np.expand_dims(out_z, 0)
                out_z_gt = np.expand_dims(out_z_gt, 0)
                # sim_mat_z = np.expand_dims(sim_mat_z, 0)
                pred = debugger.gen_colormap(out_z)
                gt = debugger.gen_colormap(out_z_gt)
                # sim_mat_c = debugger.gen_colormap(sim_mat_z)
                # gt = debugger
                tomo_z = cv2.normalize(tomo[z], None, alpha=0, beta=1, norm_type=cv2.NORM_MINMAX, dtype=cv2.CV_32F)
                # out_p = cv2.normalize(out_proj, None, alpha=0, beta=1, norm_type=cv2.NORM_MINMAX, dtype=cv2.CV_32F)
                tomo_z = np.clip(tomo_z * 255., 0, 255).astype(np.uint8)
                # out_p = np.clip(out_p * 255., 0, 255).astype(np.uint8)
                # proj_slice = np.dstack((out_p, out_p, out_p))
                # print(np.min(tomo_z), np.max(tomo_z))
                img_slice = np.dstack((tomo_z, tomo_z, tomo_z))
                # debugger.add_blend_img(img_slice, pred, 'pred_hm')
                debugger.add_slice(pred, 'pred_hm')
                # debugger.add_blend_img(img_slice, pred, 'pred_hm')
                # debugger.add_slice(sim_mat_c, 'similarity_matrix')
                # debugger.add_slice(gt, 'gt_hm')
                debugger.add_blend_img(img_slice, gt, 'gt_hm')
                debugger.add_slice(img_slice, img_id = 'pred_out')
                debugger.add_slice(img_slice, img_id = 'gt_out')
                # debugger.add_slice(proj_slice, img_id = 'project_features')
                
                if z in preds.keys():
                    slice_coords = preds[z]
                # slice_coords = preds[z]
                    # print('slice_coords', slice_coords)
                    debugger.add_particle_detection(slice_coords, 8, img_id = 'pred_out')
                if z in gts.keys():
                    slice_coords = gts[z]
                    debugger.add_particle_detection(slice_coords, 8, img_id = 'gt_out')
                if opt.debug == 4:
                    debugger.save_all_imgs(opt.debug_dir, prefix='{}'.format(iter_id), slice_num = z)

                # print('color_map_gt', gt.shape)
                # print(out_z.shape)
                # print('gts', gts)
            # print('tomo,', tomo.shape)


            # print('dets', dets)
        # print(dets.shape)
        # print(dets_gt.shape)

    def save_results(self, output, batch, results):
        reg = output['reg'] if self.opt.reg_offset else None 
        dets = tomo_decode(output['hm'], output['wh'], reg=reg, K = self.opt.K)
        dets = dets.detach().cpu().numpy().reshape(1, -1, dets.shape[2])
        return dets 