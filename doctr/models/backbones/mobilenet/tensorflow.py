# Copyright (C) 2021, Mindee.

# This program is licensed under the Apache License version 2.
# See LICENSE or go to <https://www.apache.org/licenses/LICENSE-2.0.txt> for full license details.

# Greatly inspired by https://github.com/pytorch/vision/blob/master/torchvision/models/mobilenetv3.py

import tensorflow as tf
from tensorflow.keras import layers
from tensorflow.keras.models import Sequential
from typing import Optional, Tuple, Any, Dict, List
from ...utils import conv_sequence, load_pretrained_params


__all__ = ["MobileNetV3", "mobilenet_v3_small", "mobilenet_v3_large"]


default_cfgs: Dict[str, Dict[str, Any]] = {
    'mobilenet_v3_large': {
        'input_shape': (224, 224, 3),
        'url': None
    },
    'mobilenet_v3_small': {
        'input_shape': (224, 224, 3),
        'url': None
    }
}


def hard_swish(x: tf.Tensor) -> tf.Tensor:
    return x * tf.nn.relu6(x + 3.) / 6.0


def _make_divisible(v: float, divisor: int, min_value: Optional[int] = None) -> int:
    if min_value is None:
        min_value = divisor
    new_v = max(min_value, int(v + divisor / 2) // divisor * divisor)
    # Make sure that round down does not go down by more than 10%.
    if new_v < 0.9 * v:
        new_v += divisor
    return new_v


class SqueezeExcitation(Sequential):
    """Squeeze and Excitation.
    """
    def __init__(self, chan: int, squeeze_factor: int = 4) -> None:
        super().__init__(
            [
                layers.GlobalAveragePooling2D(),
                layers.Dense(chan // squeeze_factor, activation='relu'),
                layers.Dense(chan, activation='hard_sigmoid'),
                layers.Reshape((1, 1, chan))
            ]
        )

    def call(self, inputs: tf.Tensor, **kwargs: Any) -> tf.Tensor:
        x = super().call(inputs, **kwargs)
        x = tf.math.multiply(inputs, x)
        return x


class InvertedResidualConfig:
    def __init__(
        self,
        input_channels: int,
        kernel: int,
        expanded_channels: int,
        out_channels: int,
        use_se: bool,
        activation: str,
        stride: int,
        width_mult: float = 1,
    ) -> None:
        self.input_channels = self.adjust_channels(input_channels, width_mult)
        self.kernel = kernel
        self.expanded_channels = self.adjust_channels(expanded_channels, width_mult)
        self.out_channels = self.adjust_channels(out_channels, width_mult)
        self.use_se = use_se
        self.use_hs = activation == "HS"
        self.stride = stride

    @staticmethod
    def adjust_channels(channels: int, width_mult: float):
        return _make_divisible(channels * width_mult, 8)


class InvertedResidual(layers.Layer):
    """InvertedResidual for mobilenet

    Args:
        conf: configuration object for inverted residual
    """
    def __init__(
        self,
        conf: InvertedResidualConfig,
        **kwargs: Any,
    ) -> None:
        _kwargs = {'input_shape': kwargs.pop('input_shape')} if isinstance(kwargs.get('input_shape'), tuple) else {}
        super().__init__(**kwargs)

        act_fn = hard_swish if conf.use_hs else tf.nn.relu

        self.use_res_connect = conf.stride == 1 and conf.input_channels == conf.out_channels

        _layers = []
        # expand
        if conf.expanded_channels != conf.input_channels:
            _layers.extend(conv_sequence(conf.expanded_channels, act_fn, kernel_size=1, bn=True, **_kwargs))

        # depth-wise
        _layers.extend(conv_sequence(
            conf.expanded_channels, act_fn, kernel_size=conf.kernel, strides=conf.stride, bn=True,
            groups=conf.expanded_channels,
        ))

        if conf.use_se:
            _layers.append(SqueezeExcitation(conf.expanded_channels))

        # project
        _layers.extend(conv_sequence(
            conf.out_channels, None, kernel_size=1, bn=True,
        ))

        self.block = Sequential(_layers)

    def call(
        self,
        inputs: tf.Tensor,
        **kwargs: Any,
    ) -> tf.Tensor:

        out = self.block(inputs, **kwargs)
        if self.use_res_connect:
            out = tf.add(out, inputs)

        return out


class MobileNetV3(Sequential):
    """Implements MobileNetV3, inspired from both:
    <https://github.com/xiaochus/MobileNetV3/tree/master/model>`_.
    and <https://pytorch.org/vision/stable/_modules/torchvision/models/mobilenetv3.html>`_.
    """

    def __init__(
        self,
        layout: List[InvertedResidualConfig],
        input_shape: Tuple[int, int, int],
        include_top: bool = False,
        head_chans: int = 1024,
        num_classes: int = 1000,
    ) -> None:

        _layers = [
            Sequential(conv_sequence(layout[0].input_channels, hard_swish, True, kernel_size=3, strides=2,
                       input_shape=input_shape), name="stem")
        ]

        for idx, conf in enumerate(layout):
            _layers.append(
                InvertedResidual(conf, name=f"inverted_{idx}"),
            )

        _layers.append(
            Sequential(
                conv_sequence(6 * layout[-1].out_channels, hard_swish, True, kernel_size=1),
                name="final_block"
            )
        )

        if include_top:
            _layers.extend([
                layers.GlobalAveragePooling2D(),
                layers.Dense(head_chans, activation=hard_swish),
                layers.Dropout(0.2),
                layers.Dense(num_classes, activation='softmax'),
            ])

        super().__init__(_layers)


def _mobilenet_v3(
    arch: str,
    pretrained: bool,
    input_shape: Optional[Tuple[int, int, int]] = None,
    **kwargs: Any
) -> MobileNetV3:
    input_shape = input_shape or default_cfgs[arch]['input_shape']

    # cf. Table 1 & 2 of the paper
    if arch == "mobilenet_v3_small":
        inverted_residual_setting = [
            InvertedResidualConfig(16, 3, 16, 16, True, "RE", 2),  # C1
            InvertedResidualConfig(16, 3, 72, 24, False, "RE", 2),  # C2
            InvertedResidualConfig(24, 3, 88, 24, False, "RE", 1),
            InvertedResidualConfig(24, 5, 96, 40, True, "HS", 2),  # C3
            InvertedResidualConfig(40, 5, 240, 40, True, "HS", 1),
            InvertedResidualConfig(40, 5, 240, 40, True, "HS", 1),
            InvertedResidualConfig(40, 5, 120, 48, True, "HS", 1),
            InvertedResidualConfig(48, 5, 144, 48, True, "HS", 1),
            InvertedResidualConfig(48, 5, 288, 96, True, "HS", 2),  # C4
            InvertedResidualConfig(96, 5, 576, 96, True, "HS", 1),
            InvertedResidualConfig(96, 5, 576, 96, True, "HS", 1),
        ]
        head_chans = 1024
    else:
        inverted_residual_setting = [
            InvertedResidualConfig(16, 3, 16, 16, False, "RE", 1),
            InvertedResidualConfig(16, 3, 64, 24, False, "RE", 2),  # C1
            InvertedResidualConfig(24, 3, 72, 24, False, "RE", 1),
            InvertedResidualConfig(24, 5, 72, 40, True, "RE", 2),  # C2
            InvertedResidualConfig(40, 5, 120, 40, True, "RE", 1),
            InvertedResidualConfig(40, 5, 120, 40, True, "RE", 1),
            InvertedResidualConfig(40, 3, 240, 80, False, "HS", 2),  # C3
            InvertedResidualConfig(80, 3, 200, 80, False, "HS", 1),
            InvertedResidualConfig(80, 3, 184, 80, False, "HS", 1),
            InvertedResidualConfig(80, 3, 184, 80, False, "HS", 1),
            InvertedResidualConfig(80, 3, 480, 112, True, "HS", 1),
            InvertedResidualConfig(112, 3, 672, 112, True, "HS", 1),
            InvertedResidualConfig(112, 5, 672, 160, True, "HS", 2),  # C4
            InvertedResidualConfig(160, 5, 960, 160, True, "HS", 1),
            InvertedResidualConfig(160, 5, 960, 160, True, "HS", 1),
        ]
        head_chans = 1280
    # Build the model
    model = MobileNetV3(
        inverted_residual_setting,
        input_shape,
        head_chans=head_chans,
        **kwargs,
    )
    # Load pretrained parameters
    if pretrained:
        load_pretrained_params(model, default_cfgs[arch]['url'])

    return model


def mobilenet_v3_small(pretrained: bool = False, **kwargs: Any) -> MobileNetV3:
    """MobileNetV3-Small architecture as described in
    `"Searching for MobileNetV3",
    <https://arxiv.org/pdf/1905.02244.pdf>`_.

    Example::
        >>> import tensorflow as tf
        >>> from doctr.models import mobilenetv3_large
        >>> model = mobilenetv3_small(pretrained=False)
        >>> input_tensor = tf.random.uniform(shape=[1, 512, 512, 3], maxval=1, dtype=tf.float32)
        >>> out = model(input_tensor)

    Args:
        pretrained: boolean, True if model is pretrained

    Returns:
        A  mobilenetv3_small model
    """

    return _mobilenet_v3('mobilenet_v3_small', pretrained, **kwargs)


def mobilenet_v3_large(pretrained: bool = False, **kwargs: Any) -> MobileNetV3:
    """MobileNetV3-Large architecture as described in
    `"Searching for MobileNetV3",
    <https://arxiv.org/pdf/1905.02244.pdf>`_.

    Example::
        >>> import tensorflow as tf
        >>> from doctr.models import mobilenetv3_large
        >>> model = mobilenetv3_large(pretrained=False)
        >>> input_tensor = tf.random.uniform(shape=[1, 512, 512, 3], maxval=1, dtype=tf.float32)
        >>> out = model(input_tensor)

    Args:
        pretrained: boolean, True if model is pretrained

    Returns:
        A  mobilenetv3_large model
    """
    return _mobilenet_v3('mobilenet_v3_large', pretrained, **kwargs)