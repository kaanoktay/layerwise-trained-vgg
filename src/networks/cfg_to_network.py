import torch
from torch import nn, Tensor 
from torch.optim import Adam
from torch import load as torch_load

from modules import SupervisedSidecarAutoencoder, VGG, \
  SupervisedAutoencoder

from modules import RandomMap, \
  ConvMap, DecoderMap, InterpolationMap, SidecarMap

from modules import NetworkStack

from loaders import ConfigLoader

from .layer_training_def import LayerTrainingDefinition, LayerType

from typing import List, IO

def load_layer(layer: nn.Module, path: str):
  return layer.load_state_dict(torch_load(path))

def vgg_sidecar_layer(vgg: VGG, index:int, dropout:float) -> nn.Module:
  vgg_layers, channels, img_size, _ = vgg.get_trainable_modules()[index]
  scae = SupervisedSidecarAutoencoder(vgg_layers, img_size, channels, dropout)
  return scae


def cfg_to_network(gcfg: ConfigLoader, rcfg: ConfigLoader) \
  -> List[LayerTrainingDefinition]:

  # params from run config
  num_layers = len(rcfg['layers'])
  learning_rate = rcfg['learning_rate']
  weight_decay = rcfg['weight_decay']
  color_channels = rcfg['color_channels']
  dataset_name = rcfg['dataset']
  model_path = rcfg['model_path']

  vgg_version = rcfg['vgg_version']
  vgg_dropout = rcfg['vgg_dropout']

  # params from global environment config
  device = gcfg['device']
  img_size = gcfg[f'datasets/{dataset_name}/img_size']
  num_classes = gcfg[f'datasets/{dataset_name}/num_classes']

  layer_configs = []

  # just initialize VGG, doesnt take much time
  # even when not needed
  vgg = VGG(
    num_classes=num_classes,
    dropout=vgg_dropout,
    img_size=img_size,
    vgg_version=vgg_version) 

  for id_l, layer in enumerate(rcfg['layers']):

    dropout_rate = layer['dropout_rate']
    model_type = layer['model']
    uprms = layer['upstream_params']

    # Prepare the model
    model = rcfg.switch(f'layers/{id_l}/model', {
      'AE': lambda: SupervisedAutoencoder(
        color_channels=color_channels
      ),
      'VGGn': lambda: vgg_sidecar_layer(vgg, id_l,
        dropout=dropout_rate
      ),
      'VGGlinear': lambda: vgg
    }).to(device)

    upstream = None
    # Prepare the upstream for uniform autoencoder networks
    if id_l < num_layers - 1 and model_type == 'AE':
      upstream = rcfg.switch(f'layers/{id_l}/upstream', {
        'RandomMap': lambda: RandomMap( 
          in_shape=uprms['in_shape'],
          out_shape=uprms['out_shape']
        ),
        'InterpolationMap': lambda: InterpolationMap(),
        'ConvMap': lambda: ConvMap(
          in_shape=uprms['in_shape'],
          out_shape=uprms['out_shape']
        ),
        'DecoderMap': lambda: DecoderMap(model)
      }).to(device)

    # Prepare the upstream for VGG
    elif model_type == 'VGGn':
      _, _, _, upstream_map = vgg.get_trainable_modules()[id_l]
      if upstream_map is not None:
        upstream = SidecarMap([upstream_map])

    # Prepare the optimizer / stack for various networks
    if model_type != 'VGGlinear':
      layer_type = LayerType.Stack

      # Prepare learnable scalars
      tp_alpha = nn.Parameter(torch.rand(1).to(device), requires_grad=True)
      trainable_params = [tp_alpha]
      
      # Init stack out of layers
      prev_stack = [(cfg.model, cfg.upstream) for cfg in layer_configs]
      prev_stack.append((model, upstream))
      stack = NetworkStack(prev_stack).to(device)

      # load stack tensors from pickle if required
      stack_name = layer['pretraining_load']
      if stack_name is not None:
        stack_path = '{}/{}'.format(model_path, stack_name)
        load_layer(stack, stack_path)

      # some upstream maps require training
      if upstream is not None and upstream.requires_training:
        trainable_params += list(model.parameters()) + list(upstream.parameters())
      else:
        trainable_params += model.parameters()
      

    elif model_type == 'VGGlinear':
      stack = None
      model = vgg
      trainable_params = list(vgg.classifier.parameters())
      layer_type = LayerType.VGGlinear

    optimizer = Adam(
      trainable_params,
      lr=learning_rate,
      weight_decay=weight_decay
    )

    layer_name = f'layer_{id_l}'

    layer_configs.append(
      LayerTrainingDefinition(
        layer_type=layer_type,
        layer_name=layer_name,
        num_epochs=layer['num_epoch'], 
        upstream=upstream,
        stack=stack,
        model=model,
        optimizer=optimizer,
        pretraining_store=layer['pretraining_store'],
        pretraining_load=layer['pretraining_load'],
        model_base_path=model_path,
        tp_alpha=tp_alpha
      )
    )
    # end for loop

  return layer_configs
