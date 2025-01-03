import torch
import torch.nn as nn
import torch.nn.functional as F
import math
from yolov6.layers.common import *
from yolov6.assigners.anchor_generator import generate_anchors
from yolov6.utils.general import dist2bbox


class Detect(nn.Module):
    export = False
    '''Efficient Decoupled Head for Cost-free Distillation.(FOR NANO/SMALL MODEL)
    '''
    def __init__(self, num_classes=80, num_layers=3, inplace=True, head_layers=None, use_dfl=True, reg_max=16):  # detection layer
        super().__init__()
        assert head_layers is not None
        self.nc = num_classes  # number of classes
        self.no = num_classes + 5  # number of outputs per anchor
        self.nl = num_layers  # number of detection layers
        self.grid = [torch.zeros(1)] * num_layers
        self.prior_prob = 1e-2
        self.inplace = inplace
        stride = [8, 16, 32]  # strides computed during build
        self.stride = torch.tensor(stride)
        self.use_dfl = use_dfl
        self.reg_max = reg_max
        self.proj_conv = nn.Conv2d(self.reg_max + 1, 1, 1, bias=False)
        self.grid_cell_offset = 0.5
        self.grid_cell_size = 5.0

        # Init decouple head
        self.stems = nn.ModuleList()
        self.cls_convs = nn.ModuleList()
        self.reg_convs = nn.ModuleList()
        self.cls_preds = nn.ModuleList()
        self.reg_preds_dist = nn.ModuleList()
        self.reg_preds = nn.ModuleList()

        # Efficient decoupled head layers
        for i in range(num_layers):
            idx = i*6
            self.stems.append(head_layers[idx])
            self.cls_convs.append(head_layers[idx+1])
            self.reg_convs.append(head_layers[idx+2])
            self.cls_preds.append(head_layers[idx+3])
            self.reg_preds_dist.append(head_layers[idx+4])
            self.reg_preds.append(head_layers[idx+5])

    def initialize_biases(self):

        for conv in self.cls_preds:
            b = conv.bias.view(-1, )
            b.data.fill_(-math.log((1 - self.prior_prob) / self.prior_prob))
            conv.bias = torch.nn.Parameter(b.view(-1), requires_grad=True)
            w = conv.weight
            w.data.fill_(0.)
            conv.weight = torch.nn.Parameter(w, requires_grad=True)

        for conv in self.reg_preds_dist:
            b = conv.bias.view(-1, )
            b.data.fill_(1.0)
            conv.bias = torch.nn.Parameter(b.view(-1), requires_grad=True)
            w = conv.weight
            w.data.fill_(0.)
            conv.weight = torch.nn.Parameter(w, requires_grad=True)

        for conv in self.reg_preds:
            b = conv.bias.view(-1, )
            b.data.fill_(1.0)
            conv.bias = torch.nn.Parameter(b.view(-1), requires_grad=True)
            w = conv.weight
            w.data.fill_(0.)
            conv.weight = torch.nn.Parameter(w, requires_grad=True)

        self.proj = nn.Parameter(torch.linspace(0, self.reg_max, self.reg_max + 1), requires_grad=False)
        self.proj_conv.weight = nn.Parameter(self.proj.view([1, self.reg_max + 1, 1, 1]).clone().detach(),
                                                   requires_grad=False)

    def forward(self, x):
        if self.training:
            cls_score_list = []
            reg_distri_list = []
            reg_lrtb_list = []

            for i in range(self.nl):
                x[i] = self.stems[i](x[i])
                cls_x = x[i]
                reg_x = x[i]
                cls_feat = self.cls_convs[i](cls_x)
                cls_output = self.cls_preds[i](cls_feat)
                reg_feat = self.reg_convs[i](reg_x)
                reg_output = self.reg_preds_dist[i](reg_feat)
                reg_output_lrtb = self.reg_preds[i](reg_feat)

                cls_output = torch.sigmoid(cls_output)
                cls_score_list.append(cls_output.flatten(2).permute((0, 2, 1)))
                reg_distri_list.append(reg_output.flatten(2).permute((0, 2, 1)))
                reg_lrtb_list.append(reg_output_lrtb.flatten(2).permute((0, 2, 1)))

            cls_score_list = torch.cat(cls_score_list, axis=1)
            reg_distri_list = torch.cat(reg_distri_list, axis=1)
            reg_lrtb_list = torch.cat(reg_lrtb_list, axis=1)

            return x, cls_score_list, reg_distri_list, reg_lrtb_list
        else:
            # Inference mode with separate outputs
            outputs = []  # Store separate outputs for each scale

            # Generate anchors for all scales at once
            anchor_points, stride_tensor = generate_anchors(
                x, self.stride, self.grid_cell_size, self.grid_cell_offset, 
                device=x[0].device, is_eval=True, mode='af'
            )

            # Process each detection layer
            start_idx = 0
            for i in range(self.nl):
                b, _, h, w = x[i].shape
                l = h * w
                
                # Process features through network
                feat = self.stems[i](x[i])
                cls_feat = self.cls_convs[i](feat)
                reg_feat = self.reg_convs[i](feat)
                
                # Get predictions
                cls_output = self.cls_preds[i](cls_feat)
                reg_output = self.reg_preds[i](reg_feat)
                
                # Apply sigmoid to classification outputs
                cls_output = torch.sigmoid(cls_output)
                
                # Reshape to maintain spatial dimensions
                cls_output = cls_output.permute(0, 2, 3, 1)  # [b, h, w, nc]
                reg_output = reg_output.permute(0, 2, 3, 1)  # [b, h, w, 4]
                
                # Get anchors for this scale
                end_idx = start_idx + h * w
                scale_anchor_points = anchor_points[start_idx:end_idx]
                scale_stride = stride_tensor[start_idx:end_idx]
                
                # Convert regression outputs to boxes
                pred_bboxes = dist2bbox(
                    reg_output.reshape(-1, 4), 
                    scale_anchor_points,
                    box_format='xywh'
                )
                pred_bboxes = pred_bboxes.reshape(b, h, w, 4)
                pred_bboxes *= scale_stride[0]  # Apply stride scaling
                
                # Combine predictions for this scale
                scale_output = torch.cat([
                    pred_bboxes,  # [b, h, w, 4]
                    torch.ones((b, h, w, 1), device=pred_bboxes.device),  # confidence
                    cls_output    # [b, h, w, nc]
                ], dim=-1)
                
                outputs.append(scale_output)
                start_idx = end_idx

            return outputs


def build_effidehead_layer(channels_list, num_anchors, num_classes, reg_max=16):
    head_layers = nn.Sequential(
        # stem0
        ConvBNSiLU(
            in_channels=channels_list[6],
            out_channels=channels_list[6],
            kernel_size=1,
            stride=1
        ),
        # cls_conv0
        ConvBNSiLU(
            in_channels=channels_list[6],
            out_channels=channels_list[6],
            kernel_size=3,
            stride=1
        ),
        # reg_conv0
        ConvBNSiLU(
            in_channels=channels_list[6],
            out_channels=channels_list[6],
            kernel_size=3,
            stride=1
        ),
        # cls_pred0
        nn.Conv2d(
            in_channels=channels_list[6],
            out_channels=num_classes * num_anchors,
            kernel_size=1
        ),
        # reg_pred0
        nn.Conv2d(
            in_channels=channels_list[6],
            out_channels=4 * (reg_max + num_anchors),
            kernel_size=1
        ),
        # reg_pred0_1
        nn.Conv2d(
            in_channels=channels_list[6],
            out_channels=4 * (num_anchors),
            kernel_size=1
        ),
        # stem1
        ConvBNSiLU(
            in_channels=channels_list[8],
            out_channels=channels_list[8],
            kernel_size=1,
            stride=1
        ),
        # cls_conv1
        ConvBNSiLU(
            in_channels=channels_list[8],
            out_channels=channels_list[8],
            kernel_size=3,
            stride=1
        ),
        # reg_conv1
        ConvBNSiLU(
            in_channels=channels_list[8],
            out_channels=channels_list[8],
            kernel_size=3,
            stride=1
        ),
        # cls_pred1
        nn.Conv2d(
            in_channels=channels_list[8],
            out_channels=num_classes * num_anchors,
            kernel_size=1
        ),
        # reg_pred1
        nn.Conv2d(
            in_channels=channels_list[8],
            out_channels=4 * (reg_max + num_anchors),
            kernel_size=1
        ),
        # reg_pred1_1
        nn.Conv2d(
            in_channels=channels_list[8],
            out_channels=4 * (num_anchors),
            kernel_size=1
        ),
        # stem2
        ConvBNSiLU(
            in_channels=channels_list[10],
            out_channels=channels_list[10],
            kernel_size=1,
            stride=1
        ),
        # cls_conv2
        ConvBNSiLU(
            in_channels=channels_list[10],
            out_channels=channels_list[10],
            kernel_size=3,
            stride=1
        ),
        # reg_conv2
        ConvBNSiLU(
            in_channels=channels_list[10],
            out_channels=channels_list[10],
            kernel_size=3,
            stride=1
        ),
        # cls_pred2
        nn.Conv2d(
            in_channels=channels_list[10],
            out_channels=num_classes * num_anchors,
            kernel_size=1
        ),
        # reg_pred2
        nn.Conv2d(
            in_channels=channels_list[10],
            out_channels=4 * (reg_max + num_anchors),
            kernel_size=1
        ),
        # reg_pred2_1
        nn.Conv2d(
            in_channels=channels_list[10],
            out_channels=4 * (num_anchors),
            kernel_size=1
        )
    )
    return head_layers
