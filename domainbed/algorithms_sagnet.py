# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
# Adapted from: https://github.com/hyeonseobnam/style-agnostic-networks/

import torch
import torch.nn as nn
import torch.optim as optim
import torch.utils.model_zoo as model_zoo

from domainbed import hparams_registry

model_urls = {
    'resnet18': 'https://download.pytorch.org/models/resnet18-5c106cde.pth',
    'resnet50': 'https://download.pytorch.org/models/resnet50-19c8e357.pth'
}


def conv3x3(in_planes, out_planes, stride=1):
    "3x3 convolution with padding"
    return nn.Conv2d(in_planes, out_planes, kernel_size=3, stride=stride, padding=1, bias=False)


class AdvLoss(torch.nn.Module):
    def __init__(self, eps=1e-5):
        super().__init__()
        self.eps = eps

    def forward(self, inputs):
        inputs = inputs.softmax(dim=1)
        loss = - torch.log(inputs + self.eps).mean(dim=1)
        return loss.mean()


class StyleRandomization(nn.Module):
    def __init__(self, eps=1e-5):
        super().__init__()
        self.eps = eps

    def forward(self, x):
        N, C, H, W = x.size()

        if self.training:
            x = x.view(N, C, -1)
            mean = x.mean(-1, keepdim=True)
            var = x.var(-1, keepdim=True)
            
            x = (x - mean) / (var + self.eps).sqrt()
            
            idx_swap = torch.randperm(N)
            alpha = torch.rand(N, 1, 1)
            if x.is_cuda:
                alpha = alpha.cuda()
            mean = alpha * mean + (1 - alpha) * mean[idx_swap]
            var = alpha * var + (1 - alpha) * var[idx_swap]

            x = x * (var + self.eps).sqrt() + mean
            x = x.view(N, C, H, W)

        return x


class ContentRandomization(nn.Module):
    def __init__(self, eps=1e-5):
        super().__init__()
        self.eps = eps

    def forward(self, x):
        N, C, H, W = x.size()

        if self.training:
            x = x.view(N, C, -1)
            mean = x.mean(-1, keepdim=True)
            var = x.var(-1, keepdim=True)
            
            x = (x - mean) / (var + self.eps).sqrt()
            
            idx_swap = torch.randperm(N)
            x = x[idx_swap].detach()

            x = x * (var + self.eps).sqrt() + mean
            x = x.view(N, C, H, W)

        return x


class BasicBlock(nn.Module):
    expansion = 1

    def __init__(self, inplanes, planes, stride=1, downsample=None):
        super().__init__()
        self.conv1 = nn.Conv2d(inplanes, planes, kernel_size=3, padding=1, stride=stride, bias=False)
        self.bn1 = nn.BatchNorm2d(planes)
        self.conv2 = nn.Conv2d(planes, planes, kernel_size=3, padding=1, bias=False)
        self.bn2 = nn.BatchNorm2d(planes)
        self.relu = nn.ReLU(inplace=True)
        self.downsample = downsample
        self.stride = stride

    def forward(self, x):
        residual = x

        out = self.conv1(x)
        out = self.bn1(out)
        out = self.relu(out)

        out = self.conv2(out)
        out = self.bn2(out)

        if self.downsample is not None:
            residual = self.downsample(x)

        out += residual
        out = self.relu(out)

        return out


class Bottleneck(nn.Module):
    expansion = 4

    def __init__(self, inplanes, planes, stride=1, downsample=None):
        super().__init__()
        self.conv1 = nn.Conv2d(inplanes, planes, kernel_size=1, bias=False)
        self.bn1 = nn.BatchNorm2d(planes)
        self.conv2 = nn.Conv2d(planes, planes, kernel_size=3, stride=stride,
                               padding=1, bias=False)
        self.bn2 = nn.BatchNorm2d(planes)
        self.conv3 = nn.Conv2d(planes, planes * 4, kernel_size=1, bias=False)
        self.bn3 = nn.BatchNorm2d(planes * 4)
        self.relu = nn.ReLU(inplace=True)
        self.downsample = downsample
        self.stride = stride

    def forward(self, x):
        residual = x

        out = self.conv1(x)
        out = self.bn1(out)
        out = self.relu(out)

        out = self.conv2(out)
        out = self.bn2(out)
        out = self.relu(out)

        out = self.conv3(out)
        out = self.bn3(out)

        if self.downsample is not None:
            residual = self.downsample(x)
            
        out += residual
        out = self.relu(out)

        return out


class ResNet(nn.Module):

    def __init__(self, block, layers, num_classes=1000, drop=0, sagnet=True, style_stage=3):
        super().__init__()
        
        self.drop = drop
        self.sagnet = sagnet
        self.style_stage = style_stage

        self.inplanes = 64
        self.conv1 = nn.Conv2d(3, 64, kernel_size=7, stride=2, padding=3, bias=False)
        self.bn1 = nn.BatchNorm2d(64)
        self.relu = nn.ReLU(inplace=True)
        self.maxpool = nn.MaxPool2d(kernel_size=3, stride=2, padding=1)
        
        self.layer1 = self._make_layer(block, 64, layers[0])
        self.layer2 = self._make_layer(block, 128, layers[1], stride=2)
        self.layer3 = self._make_layer(block, 256, layers[2], stride=2)
        self.layer4 = self._make_layer(block, 512, layers[3], stride=2)
        
        self.avgpool = nn.AdaptiveAvgPool2d(1)
        self.dropout = nn.Dropout(self.drop)
        self.fc = nn.Linear(512 * block.expansion, num_classes)

        if self.sagnet:
            # randomizations
            self.style_randomization = StyleRandomization()
            self.content_randomization = ContentRandomization()
            
            # style-biased network
            style_layers = []
            if style_stage == 1:
                self.inplanes = 64
                style_layers += [self._make_layer(block, 64, layers[0])]
            if style_stage <= 2:
                self.inplanes = 64 * block.expansion
                style_layers += [self._make_layer(block, 128, layers[1], stride=2)]
            if style_stage <= 3:
                self.inplanes = 128 * block.expansion
                style_layers += [self._make_layer(block, 256, layers[2], stride=2)]
            if style_stage <= 4:
                self.inplanes = 256 * block.expansion
                style_layers += [self._make_layer(block, 512, layers[3], stride=2)]
            self.style_net = nn.Sequential(*style_layers)
            
            self.style_avgpool = nn.AdaptiveAvgPool2d(1)
            self.style_dropout = nn.Dropout(self.drop)
            self.style_fc = nn.Linear(512 * block.expansion, num_classes)

        # init weights
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                nn.init.kaiming_normal_(m.weight, mode='fan_out', nonlinearity='relu')
            if isinstance(m, nn.BatchNorm2d):
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)

    def adv_params(self):
        params = []
        layers = [self.bn1, self.layer1, self.layer2, self.layer3, self.layer4]
        for layer in layers[:self.style_stage]:
            for m in layer.modules():
                if isinstance(m, nn.BatchNorm2d):
                    params += [p for p in m.parameters()]
        return params

    def style_params(self):
        params = []
        for m in [self.style_net, self.style_fc]:
            params += [p for p in m.parameters()]
        return params

    def _make_layer(self, block, planes, blocks, stride=1):
        downsample = None
        if stride != 1 or self.inplanes != planes * block.expansion:
            if stride != 1:
                downsample = nn.Sequential(
                    nn.Conv2d(self.inplanes, planes * block.expansion, stride=stride,
                                   kernel_size=1, bias=False),
                    nn.BatchNorm2d(planes * block.expansion),
                )
            else:
                downsample = nn.Sequential(
                    nn.Conv2d(self.inplanes, planes * block.expansion,
                                   kernel_size=1, bias=False),
                    nn.BatchNorm2d(planes * block.expansion),
                )

        layers = []
        layers.append(block(self.inplanes, planes, stride, downsample))
        self.inplanes = planes * block.expansion
        for i in range(1, blocks):
            layers.append(block(self.inplanes, planes))
        return nn.Sequential(*layers)

    def forward(self, x):
        x = self.conv1(x)
        x = self.bn1(x)
        x = self.relu(x)
        x = self.maxpool(x)

        for i, layer in enumerate([self.layer1, self.layer2, self.layer3, self.layer4]):
            if self.sagnet and i + 1 == self.style_stage:
                # randomization
                x_style = self.content_randomization(x)
                x = self.style_randomization(x)
            x = layer(x)

        # content output 
        feat = self.avgpool(x)
        feat = feat.view(x.size(0), -1)
        feat = self.dropout(feat)
        y = self.fc(feat)
    
        if self.sagnet:
            # style output
            x_style = self.style_net(x_style)
            feat = self.style_avgpool(x_style)
            feat = feat.view(feat.size(0), -1)
            feat = self.style_dropout(feat)
            y_style = self.style_fc(feat)
        else:
            y_style = None

        return y, y_style


def sag_resnet(depth, pretrained=False, **kwargs):
    if depth == 18:
        model = ResNet(BasicBlock, [2, 2, 2, 2], **kwargs)
    elif depth == 50:
        model = ResNet(Bottleneck, [3, 4, 6, 3], **kwargs)
    else:
        raise NotImplementedError

    if pretrained:
        model_url = model_urls['resnet' + str(depth)]
        # print('load a pretrained model from {}'.format(model_url))
    
        states = model_zoo.load_url(model_url)
        states.pop('fc.weight')
        states.pop('fc.bias')
        model.load_state_dict(states, strict=False)
    
        if model.sagnet:
            states_style = {}
            for i in range(model.style_stage, 5):
                for k, v in states.items():
                    if k.startswith('layer' + str(i)):
                        states_style[str(i - model.style_stage) + k[6:]] = v
            model.style_net.load_state_dict(states_style)
    
    return model


class SagNet(torch.nn.Module):
    def __init__(self, input_shape, num_classes, num_domains, hparams):
        super(SagNet, self).__init__()
        # persistent args
        self.sag_args_w_adv = hparams['sag_w_adv']
        self.sag_args_clip_adv = hparams['sag_clip_adv']
        self.sag_args_sagnet = True # always True

        self.model = sag_resnet(depth=18 if hparams['resnet18'] else 50,
                        pretrained=True,
                        num_classes=num_classes,
                        drop=hparams['resnet_dropout'],
                        sagnet=self.sag_args_sagnet,
                        style_stage=hparams['sag_style_stage'])

        # Set hyperparams
        optim_hyperparams = {'lr': hparams['lr'], 
                             'weight_decay': hparams['weight_decay'],
                             'momentum': hparams['sag_momentum']}
        if hparams['sag_scheduler'] == 'step':
            Scheduler = optim.lr_scheduler.MultiStepLR
            sch_hyperparams = {'milestones': [1000, 2000, 3000, 4000],
                               'gamma': hparams['sag_gamma']}
        elif hparams['sag_scheduler'] == 'cosine':
            Scheduler = optim.lr_scheduler.CosineAnnealingLR
            sch_hyperparams = {'T_max': 5001}
        
        # Main learning
        params = self.model.parameters()
        self.optimizer = optim.SGD(params, **optim_hyperparams)
        self.scheduler = Scheduler(self.optimizer, **sch_hyperparams)
        self.criterion = torch.nn.CrossEntropyLoss()
        
        if self.sag_args_sagnet:
            # Style learning
            params_style = self.model.style_params()
            self.optimizer_style = optim.SGD(params_style, **optim_hyperparams)
            self.scheduler_style = Scheduler(self.optimizer_style, **sch_hyperparams)
            self.criterion_style = torch.nn.CrossEntropyLoss()
            
            # Adversarial learning
            params_adv = self.model.adv_params()
            self.optimizer_adv = optim.SGD(params_adv, **optim_hyperparams)
            self.scheduler_adv = Scheduler(self.optimizer_adv, **sch_hyperparams)
            self.criterion_adv = AdvLoss()

    def update(self, minibatches):
        data = torch.cat([x for x, _ in minibatches])
        label = torch.cat([y for _, y in minibatches])
        perm = torch.randperm(len(data))
        data = data[perm]
        label = label[perm]

        # forward
        y, y_style = self.model(data)

        if self.sag_args_sagnet:
            # learn style
            loss_style = self.criterion(y_style, label)
            self.optimizer_style.zero_grad()
            loss_style.backward(retain_graph=True)
        
            # learn style_adv
            loss_adv = self.sag_args_w_adv * self.criterion_adv(y_style)
            self.optimizer_adv.zero_grad()
            loss_adv.backward(retain_graph=True)
            if self.sag_args_clip_adv is not None:
                torch.nn.utils.clip_grad_norm_(self.model.adv_params(),
                                               self.sag_args_clip_adv)

        # learn content
        loss = self.criterion(y, label)
        self.optimizer.zero_grad()
        loss.backward()
        
        self.optimizer_style.step()
        self.optimizer_adv.step()
        self.optimizer.step()
        
        self.scheduler.step()
        if self.sag_args_sagnet:
            self.scheduler_style.step()
            self.scheduler_adv.step()

        return {'loss_style': loss_style.item(),
                'loss_adv': loss_adv.item(),
                'loss': loss.item()}

    def predict(self, x):
        return self.model(x)[0]
