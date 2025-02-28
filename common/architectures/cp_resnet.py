# coding: utf-8
import math

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch._six import container_abcs
from torch.nn.modules.conv import _ConvNd
from torch.utils.checkpoint import checkpoint_sequential
import numpy as np
import collections.abc
from librosa.filters import mel as librosa_mel_fn

from itertools import repeat

def update_dict(d, u):
    for k, v in u.items():
        if isinstance(v, collections.abc.Mapping):
            d[k] = update_dict(d.get(k, {}), v)
        else:
            d[k] = v
    return d


def _ntuple(n):
    def parse(x):
        if isinstance(x, container_abcs.Iterable):
            return x
        return tuple(repeat(x, n))

    return parse


_single = _ntuple(1)
_pair = _ntuple(2)
_triple = _ntuple(3)
_quadruple = _ntuple(4)


class Conv2dDamped(_ConvNd):
    r"""Applies a 2D FREQUENCY DAMPED convolution over an input signal composed of several input
    planes.
    """

    def __init__(self, in_channels, out_channels, kernel_size, stride=1,
                 padding=0, dilation=1, groups=1,
                 bias=True):
        kernel_size = _pair(kernel_size)
        stride = _pair(stride)
        padding = _pair(padding)
        dilation = _pair(dilation)
        try:
            super(Conv2dDamped, self).__init__(
                in_channels, out_channels, kernel_size, stride, padding, dilation,
                False, _pair(0), groups, bias)
        except:
            super(Conv2dDamped, self).__init__(
                in_channels, out_channels, kernel_size, stride, padding, dilation,
                False, _pair(0), groups, bias, padding_mode='zeros')

    def conv2d_forward(self, input, weight):

        damper = get_damper(weight)
        # print(damper)
        return F.conv2d(input, weight * damper, self.bias, self.stride,
                        self.padding, self.dilation, self.groups)

    def forward(self, input):
        return self.conv2d_forward(input, self.weight)


cach_damp = {}


def get_damper(w):
    k = (w.shape[2], w.shape[3])
    if cach_damp.get(k) is None:
        t = torch.ones_like(w[0, 0]).reshape(1, 1, w.shape[2], w.shape[3])
        center2 = (w.shape[2] - 1.) / 2
        center3 = (w.shape[3] - 1.) / 2
        minscale = 0.01
        if center2 >= 1:
            for i in range(w.shape[2]):
                distance = np.abs(i - center2)
                sacale = -(1 - minscale) * distance / center2 + 1.
                t[:, :, i, :] *= sacale
        # if center2 >= 1:
        #     for i in range(w.shape[3]):
        #         distance = np.abs(i - center3)
        #         sacale = -(1 - minscale) * distance/center3 + 1.
        #         t[:, :, :, i] *= sacale
        cach_damp[k] = t.detach()
    return cach_damp.get(k)


def initialize_weights(module):
    if isinstance(module, Conv2dDamped):
        nn.init.kaiming_normal_(module.weight.data, mode='fan_in', nonlinearity="relu")

        # nn.init.kaiming_normal_(module.weight.data, mode='fan_out')
    elif isinstance(module, nn.BatchNorm2d):
        module.weight.data.fill_(1)
        module.bias.data.zero_()
    elif isinstance(module, nn.Linear):
        torch.nn.init.xavier_uniform(module.weight)
        module.bias.data.zero_()


layer_index_total = 0


def initialize_weights_fixup(module):
    if isinstance(module, BasicBlock):
        # He init, rescaled by Fixup multiplier
        b = module
        n = b.conv1.kernel_size[0] * b.conv1.kernel_size[1] * b.conv1.out_channels
        print(b.layer_index, math.sqrt(2. / n), layer_index_total ** (-0.5))
        b.conv1.weight.data.normal_(0, (layer_index_total ** (-0.5)) * math.sqrt(2. / n))
        b.conv2.weight.data.zero_()
        if b.shortcut._modules.get('conv') is not None:
            convShortcut = b.shortcut._modules.get('conv')
            n = convShortcut.kernel_size[0] * convShortcut.kernel_size[1] * convShortcut.out_channels
            convShortcut.weight.data.normal_(0, math.sqrt(2. / n))
    if isinstance(module, Conv2dDamped):
        pass
        # nn.init.kaiming_normal_(module.weight.data, mode='fan_in', nonlinearity="relu")
        # nn.init.kaiming_normal_(module.weight.data, mode='fan_out')
    elif isinstance(module, nn.BatchNorm2d):
        module.weight.data.fill_(1)
        module.bias.data.zero_()
    elif isinstance(module, nn.Linear):
        module.bias.data.zero_()


first_RUN = True


def calc_padding(kernal):
    try:
        return kernal // 3
    except TypeError:
        return [k // 3 for k in kernal]



class BasicBlock(nn.Module):
    expansion = 1

    def __init__(self, in_channels, out_channels, stride, k1=3, k2=3):
        super(BasicBlock, self).__init__()
        global layer_index_total
        self.layer_index = layer_index_total
        layer_index_total = layer_index_total + 1
        self.conv1 = Conv2dDamped(
            in_channels,
            out_channels,
            kernel_size=k1,
            stride=stride,  # downsample with first conv
            padding=calc_padding(k1),
            bias=False)
        self.bn1 = nn.BatchNorm2d(out_channels)
        self.conv2 = Conv2dDamped(
            out_channels,
            out_channels,
            kernel_size=k2,
            stride=1,
            padding=calc_padding(k2),
            bias=False)
        self.bn2 = nn.BatchNorm2d(out_channels)

        self.shortcut = nn.Sequential()
        if in_channels != out_channels:
            self.shortcut.add_module(
                'conv',
                Conv2dDamped(
                    in_channels,
                    out_channels,
                    kernel_size=1,
                    stride=stride,  # downsample
                    padding=0,
                    bias=False))
            self.shortcut.add_module('bn', nn.BatchNorm2d(out_channels))  # BN

    def forward(self, x):
        y = F.relu(self.bn1(self.conv1(x)), inplace=True)
        y = self.bn2(self.conv2(y))
        y += self.shortcut(x)
        y = F.relu(y, inplace=True)  # apply ReLU after addition
        return y


class Network(nn.Module):
    def __init__(self, config):
        super(Network, self).__init__()

        input_shape = config['input_shape']
        n_classes = config['n_classes']

        base_channels = config['base_channels']
        block_type = config['block_type']
        depth = config['depth']
        self.pooling_padding = config.get("pooling_padding", 0) or 0
        self.use_raw_spectograms = config.get("use_raw_spectograms") or False
        self.apply_softmax = config.get("apply_softmax") or False
        self.return_embed = config.get("return_embed") or False

        assert block_type in ['basic', 'bottleneck']
        if self.use_raw_spectograms:
            mel_basis = librosa_mel_fn(
                22050, 2048, 256)
            mel_basis = torch.from_numpy(mel_basis).float()
            self.register_buffer('mel_basis', mel_basis)
        if block_type == 'basic':
            block = BasicBlock
            n_blocks_per_stage = (depth - 2) // 6
            assert n_blocks_per_stage * 6 + 2 == depth
        else:
            raise NotImplementedError('BottleneckBlock not implemented')
            block = BottleneckBlock
            n_blocks_per_stage = (depth - 2) // 9
            assert n_blocks_per_stage * 9 + 2 == depth
        n_blocks_per_stage = [n_blocks_per_stage, n_blocks_per_stage, n_blocks_per_stage]

        if config.get("n_blocks_per_stage") is not None:
            print(
                "n_blocks_per_stage is specified ignoring the depth param, nc=" + str(config.get("n_channels")))
            n_blocks_per_stage = config.get("n_blocks_per_stage")

        n_channels = config.get("n_channels")
        if n_channels is None:
            n_channels = [
                base_channels,
                base_channels * 2 * block.expansion,
                base_channels * 4 * block.expansion
            ]
        if config.get("grow_a_lot"):
            n_channels[2] = base_channels * 8 * block.expansion

        self.in_c = nn.Sequential(Conv2dDamped(
            input_shape[1],
            n_channels[0],
            kernel_size=5,
            stride=2,
            padding=1,
            bias=False),
            nn.BatchNorm2d(n_channels[0]),
            nn.ReLU(True)
        )
        self.stage1 = self._make_stage(
            n_channels[0], n_channels[0], n_blocks_per_stage[0], block, stride=1, maxpool=config['stage1']['maxpool'],
            k1s=config['stage1']['k1s'], k2s=config['stage1']['k2s'])
        if n_blocks_per_stage[1] == 0:
            self.stage2 = nn.Sequential()
            n_channels[1] = n_channels[0]
            print("WARNING: stage2 removed")
        else:
            self.stage2 = self._make_stage(
                n_channels[0], n_channels[1], n_blocks_per_stage[1], block, stride=1, maxpool=config['stage2']['maxpool'],
                k1s=config['stage2']['k1s'], k2s=config['stage2']['k2s'])
        if n_blocks_per_stage[2] == 0:
            self.stage3 = nn.Sequential()
            n_channels[2] = n_channels[1]
            print("WARNING: stage3 removed")
        else:
            self.stage3 = self._make_stage(
                n_channels[1], n_channels[2], n_blocks_per_stage[2], block, stride=1, maxpool=config['stage3']['maxpool'],
                k1s=config['stage3']['k1s'], k2s=config['stage3']['k2s'])

        ff_list = []
        if config.get("attention_avg"):
            if config.get("attention_avg") == "sum_all":
                ff_list.append(AttentionAvg(n_channels[2], n_classes, sum_all=True))
            else:
                ff_list.append(AttentionAvg(n_channels[2], n_classes, sum_all=False))
        else:
            ff_list += [Conv2dDamped(
                n_channels[2],
                n_classes,
                kernel_size=1,
                stride=1,
                padding=0,
                bias=False),
                nn.BatchNorm2d(n_classes),
            ]

        self.stop_before_global_avg_pooling = False
        if config.get("stop_before_global_avg_pooling"):
            self.stop_before_global_avg_pooling = True
        else:
            ff_list.append(nn.AdaptiveAvgPool2d((1, 1)))

        self.feed_forward = nn.Sequential(
            *ff_list
        )
        # # compute conv feature size
        # with torch.no_grad():
        #     self.feature_size = self._forward_conv(
        #         torch.zeros(*input_shape)).view(-1).shape[0]
        #
        # self.fc = nn.Linear(self.feature_size, n_classes)

        # initialize weights
        if config.get("weight_init") == "fixup":
            self.apply(initialize_weights)
            if isinstance(self.feed_forward[0], Conv2dDamped):
                self.feed_forward[0].weight.data.zero_()
            self.apply(initialize_weights_fixup)
        else:
            self.apply(initialize_weights)
        self.use_check_point = config.get("use_check_point") or False

    def _make_stage(self, in_channels, out_channels, n_blocks, block, stride, maxpool=set(), k1s=[3, 3, 3, 3, 3, 3],
                    k2s=[3, 3, 3, 3, 3, 3]):
        stage = nn.Sequential()
        if 0 in maxpool:
            stage.add_module("maxpool{}_{}".format(0, 0)
                             , nn.MaxPool2d(2, 2, padding=self.pooling_padding))
        for index in range(n_blocks):
            stage.add_module('block{}'.format(index + 1),
                             block(in_channels,
                                   out_channels,
                                   stride=stride, k1=k1s[index], k2=k2s[index]))

            in_channels = out_channels
            stride = 1
            # if index + 1 in maxpool:
            for m_i, mp_pos in enumerate(maxpool):
                if index + 1 == mp_pos:
                    stage.add_module("maxpool{}_{}".format(index + 1, m_i)
                                     , nn.MaxPool2d(2, 2, padding=self.pooling_padding))
        return stage

    def half_damper(self):
        global cach_damp
        for k in cach_damp.keys():
            cach_damp[k] = cach_damp[k].half()

    def _forward_conv(self, x):
        global first_RUN

        if first_RUN: print("x:", x.size())
        x = self.in_c(x)
        if first_RUN: print("in_c:", x.size())

        if self.use_check_point:
            if first_RUN: print("use_check_point:", x.size())
            return checkpoint_sequential([self.stage1, self.stage2, self.stage3], 3,
                                         (x))
        x = self.stage1(x)

        if first_RUN: print("stage1:", x.size())
        x = self.stage2(x)
        if first_RUN: print("stage2:", x.size())
        x = self.stage3(x)
        if first_RUN: print("stage3:", x.size())
        return x

    def forward(self, x):
        global first_RUN
        if self.use_raw_spectograms:
            if first_RUN: print("raw_x:", x.size())
            x = torch.log10(torch.sqrt((x * x).sum(dim=3)))
            if first_RUN: print("log10_x:", x.size())
            x = torch.matmul(self.mel_basis, x)
            if first_RUN: print("mel_basis_x:", x.size())
            x = x.unsqueeze(1)
        e = self._forward_conv(x)
        features = x
        x = self.feed_forward(e)
        if first_RUN: print("feed_forward:", x.size())
        if self.stop_before_global_avg_pooling:
            first_RUN = False
            return x
        logit = x.squeeze(2).squeeze(2)

        if first_RUN: print("logit:", logit.size())
        if self.apply_softmax:
            logit = torch.softmax(logit, 1)
        first_RUN = False
        if self.return_embed:
            return logit, features
        return logit, e




def get_model_based_on_rho(rho_t=12, rho_f=12, base_channels=128, blocks='444', n_classes=10, arch="cp_speech_resnet", config_only=False, input_shape=(10, 1, -1, -1), model_config_overrides={}):
    ekrf_fr = rho_f - 7
    ekrf_time = rho_t - 7
    global model_config
    model_config = {
        "arch": arch,
        "base_channels": base_channels,
        "block_type": "basic",
        "depth": 26,
        "input_shape": input_shape,
        "n_blocks_per_stage": [int(blocks[0]), int(blocks[1]), int(blocks[2])],
        "multi_label": False,
        "n_classes": n_classes,
        "prediction_threshold": 0.4,
        "stage1": {"maxpool": [1,],
                   "k1s": [3, (3 - (-ekrf_fr > 6) * 2, 3 - (-ekrf_time > 6) * 2),
                           (3 - (-ekrf_fr > 4) * 2, 3 - (-ekrf_time > 4) * 2),
                           (3 - (-ekrf_fr > 2) * 2, 3 - (-ekrf_time > 2) * 2)],
                   "k2s": [1,
                           (3 - (-ekrf_fr > 5) * 2, 3 - (-ekrf_time > 5) * 2),
                           (3 - (-ekrf_fr > 3) * 2, 3 - (-ekrf_time > 3) * 2),
                           (3 - (-ekrf_fr > 1) * 2, 3 - (-ekrf_time > 1) * 2)]},

        "stage2": {"maxpool": [],
                   "k1s": [(3 - (-ekrf_fr > 0) * 2, 3 - (-ekrf_time > 0) * 2),
                           (1 + (ekrf_fr > 1) * 2, 1 + (ekrf_time > 1) * 2),
                           (1 + (ekrf_fr > 3) * 2, 1 + (ekrf_time > 3) * 2),
                           (1 + (ekrf_fr > 5) * 2, 1 + (ekrf_time > 5) * 2)],
                   "k2s": [(1 + (ekrf_fr > 0) * 2, 1 + (ekrf_time > 0) * 2),
                           (1 + (ekrf_fr > 2) * 2, 1 + (ekrf_time > 2) * 2),
                           (1 + (ekrf_fr > 4) * 2, 1 + (ekrf_time > 4) * 2),
                           (1 + (ekrf_fr > 6) * 2, 1 + (ekrf_time > 6) * 2)]},
        "stage3": {"maxpool": [],
                   "k1s": [(1 + (ekrf_fr > 7) * 2, 1 + (ekrf_time > 7) * 2),
                           (1 + (ekrf_fr > 9) * 2, 1 + (ekrf_time > 9) * 2),
                           (1 + (ekrf_fr > 11) * 2, 1 + (ekrf_time > 11) * 2),
                           (1 + (ekrf_fr > 13) * 2, 1 + (ekrf_time > 13) * 2)],
                   "k2s": [(1 + (ekrf_fr > 8) * 2, 1 + (ekrf_time > 8) * 2),
                           (1 + (ekrf_fr > 10) * 2, 1 + (ekrf_time > 10) * 2),
                           (1 + (ekrf_fr > 12) * 2, 1 + (ekrf_time > 12) * 2),
                           (1 + (ekrf_fr > 14) * 2, 1 + (ekrf_time > 14) * 2)]},
        "block_type": "basic",
        "use_bn": True,
        "weight_init": "somethingelse"#"fixup"
    }
   # override model_config
    if config_only:
        return model_config

    return Network(model_config)

from functools import lru_cache
model_config=None
import numpy as np
def getk(i):
    k=i
    nblock_per_stage=(model_config['depth']-2)//6
    i=(k-1)//(nblock_per_stage*2)
    "stage%d"%(i+1),nblock_per_stage,'k%ds'%((k+1)%2+1),((k-1)%(nblock_per_stage*2))//2
    return np.array(model_config["stage%d"%(i+1)]['k%ds'%((k+1)%2+1)][((k-1)%(nblock_per_stage*2))//2])

def gets(i):
    k=i
    if k%2==1:
        return 1
    nblock_per_stage=(model_config['depth']-2)//6
    i=(k-1)//(nblock_per_stage*2)
    "stage%d"%(i+1),nblock_per_stage,'k%ds'%((k+1)%2+1),((k)%(nblock_per_stage*2))//2
    if (((k-1)%(nblock_per_stage*2))//2 + 1) in set(model_config["stage%d"%(i+1)]['maxpool']):
        return 2
    return 1

@lru_cache(maxsize=None)
def maxrf(i):
    if i==0:
        return 2,5 # starting RF
    s,rf=maxrf(i-1)
    s=s*gets(i)
    rf= rf+ (getk(i)-1)*s
    return s,rf

maxrf.cache_clear()
