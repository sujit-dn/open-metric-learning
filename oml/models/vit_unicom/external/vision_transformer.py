# type: ignore
# flake8: noqa

import math
import warnings

import torch
import torch.nn as nn
from torch.utils.checkpoint import checkpoint
from torchvision.transforms import (
    CenterCrop,
    Compose,
    InterpolationMode,
    Normalize,
    Resize,
    ToTensor,
)

# ============== CODE FROM TIMM ==============
# https://github.com/huggingface/pytorch-image-models/blob/
# c241081251323dfc5e8dc799d49740c48cc9096f/timm/layers/weight_init.py#L8


def _trunc_normal_(tensor, mean, std, a, b):
    # Cut & paste from PyTorch official master until it's in a few official releases - RW
    # Method based on https://people.sc.fsu.edu/~jburkardt/presentations/truncated_normal.pdf
    def norm_cdf(x):
        # Computes standard normal cumulative distribution function
        return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0

    if (mean < a - 2 * std) or (mean > b + 2 * std):
        warnings.warn(
            "mean is more than 2 std from [a, b] in nn.init.trunc_normal_. "
            "The distribution of values may be incorrect.",
            stacklevel=2,
        )

    # Values are generated by using a truncated uniform distribution and
    # then using the inverse CDF for the normal distribution.
    # Get upper and lower cdf values
    l = norm_cdf((a - mean) / std)
    u = norm_cdf((b - mean) / std)

    # Uniformly fill tensor with values from [l, u], then translate to
    # [2l-1, 2u-1].
    tensor.uniform_(2 * l - 1, 2 * u - 1)

    # Use inverse cdf transform for normal distribution to get truncated
    # standard normal
    tensor.erfinv_()

    # Transform to proper mean, std
    tensor.mul_(std * math.sqrt(2.0))
    tensor.add_(mean)

    # Clamp to ensure it's in the proper range
    tensor.clamp_(min=a, max=b)
    return tensor


def trunc_normal_(tensor, mean=0.0, std=1.0, a=-2.0, b=2.0):
    r"""Fills the input Tensor with values drawn from a truncated
    normal distribution. The values are effectively drawn from the
    normal distribution :math:`\mathcal{N}(\text{mean}, \text{std}^2)`
    with values outside :math:`[a, b]` redrawn until they are within
    the bounds. The method used for generating the random values works
    best when :math:`a \leq \text{mean} \leq b`.

    NOTE: this impl is similar to the PyTorch trunc_normal_, the bounds [a, b] are
    applied while sampling the normal with mean/std applied, therefore a, b args
    should be adjusted to match the range of mean, std args.

    Args:
        tensor: an n-dimensional `torch.Tensor`
        mean: the mean of the normal distribution
        std: the standard deviation of the normal distribution
        a: the minimum cutoff value
        b: the maximum cutoff value
    """
    with torch.no_grad():
        return _trunc_normal_(tensor, mean, std, a, b)


def drop_path(x, drop_prob: float = 0.0, training: bool = False, scale_by_keep: bool = True):
    """Drop paths (Stochastic Depth) per sample (when applied in main path of residual blocks).

    This is the same as the DropConnect impl I created for EfficientNet, etc networks, however,
    the original name is misleading as 'Drop Connect' is a different form of dropout in a separate paper...
    See discussion: https://github.com/tensorflow/tpu/issues/494#issuecomment-532968956 ... I've opted for
    changing the layer and argument names to 'drop path' rather than mix DropConnect as a layer name and use
    'survival rate' as the argument.

    """
    if drop_prob == 0.0 or not training:
        return x
    keep_prob = 1 - drop_prob
    shape = (x.shape[0],) + (1,) * (x.ndim - 1)  # work with diff dim tensors, not just 2D ConvNets
    random_tensor = x.new_empty(shape).bernoulli_(keep_prob)
    if keep_prob > 0.0 and scale_by_keep:
        random_tensor.div_(keep_prob)
    return x * random_tensor


class DropPath(nn.Module):
    """Drop paths (Stochastic Depth) per sample  (when applied in main path of residual blocks)."""

    def __init__(self, drop_prob: float = 0.0, scale_by_keep: bool = True):
        super(DropPath, self).__init__()
        self.drop_prob = drop_prob
        self.scale_by_keep = scale_by_keep

    def forward(self, x):
        return drop_path(x, self.drop_prob, self.training, self.scale_by_keep)

    def extra_repr(self):
        return f"drop_prob={round(self.drop_prob, 3):0.3f}"


# ============== CODE FROM UNICOM ==============
# https://github.com/deepglint/unicom/blob/main/unicom/vision_transformer.py


class VisionTransformer(nn.Module):
    def __init__(
        self,
        input_size=224,
        patch_size=32,
        in_channels=3,
        dim=768,
        embedding_size=768,
        depth=12,
        num_heads=12,
        mlp_ratio=4,
        drop_path_rate=0.0,
        using_checkpoint=True,
    ):
        super().__init__()
        self.dim = dim
        self.patch_embed = PatchEmbedding(
            input_size,
            patch_size,
            in_channels,
            dim,
        )
        self.pos_embed = nn.Parameter(torch.zeros(1, self.patch_embed.num_patches, dim))
        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, depth)]

        self.blocks = nn.ModuleList(
            [
                Block(dim, num_heads, mlp_ratio, dpr[i], self.patch_embed.num_patches, using_checkpoint)
                for i in range(depth)
            ]
        )
        self.norm = nn.LayerNorm(dim)

        self.feature = nn.Sequential(
            nn.Linear(dim * self.patch_embed.num_patches, dim, False),
            nn.BatchNorm1d(dim, eps=2e-5),
            nn.Linear(dim, embedding_size, False),
            nn.BatchNorm1d(embedding_size, eps=2e-5),
        )

        trunc_normal_(self.pos_embed, std=0.02)
        self.apply(self._init_weights)
        self.extra_gflops = 0.0
        for _block in self.blocks:
            self.extra_gflops += _block.extra_gflops

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=0.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)

    def forward_features(self, x):
        B = x.shape[0]
        x = self.patch_embed(x)
        x = x + self.pos_embed
        for func in self.blocks:
            x = func(x)
        x = self.norm(x.float())
        return torch.reshape(x, (B, self.patch_embed.num_patches * self.dim))

    def forward(self, x):
        x = self.forward_features(x)
        x = self.feature(x)
        return x


class Mlp(nn.Module):
    def __init__(self, dim, dim_hidden):
        super().__init__()
        self.fc1 = nn.Linear(dim, dim_hidden)
        self.act = nn.ReLU6()
        self.fc2 = nn.Linear(dim_hidden, dim)

    def forward(self, x):
        x = self.fc1(x)
        x = self.act(x)
        x = self.fc2(x)
        return x


class Attention(nn.Module):
    def __init__(self, dim, num_heads):
        super().__init__()
        self.num_heads = num_heads
        head_dim = dim // num_heads
        self.scale = head_dim**-0.5
        self.qkv = nn.Linear(dim, dim * 3, bias=False)
        self.proj = nn.Linear(dim, dim)

    def forward(self, x):
        with torch.cuda.amp.autocast(True):
            B, L, D = x.shape
            qkv = self.qkv(x).reshape(B, L, 3, self.num_heads, D // self.num_heads).permute(2, 0, 3, 1, 4)
        with torch.cuda.amp.autocast(False):
            q, k, v = qkv[0].float(), qkv[1].float(), qkv[2].float()
            attn = (q @ k.transpose(-2, -1)) * self.scale
            attn = attn.softmax(dim=-1)
            x = (attn @ v).transpose(1, 2).reshape(B, L, D)
        with torch.cuda.amp.autocast(True):
            x = self.proj(x)
        return x


class Block(nn.Module):
    def __init__(
        self,
        dim: int,
        num_heads: int,
        mlp_ratio: int = 4,
        drop_path: float = 0.0,
        patch_n: int = 32,
        using_checkpoint=False,
    ):
        super().__init__()
        self.using_checkpoint = using_checkpoint
        self.norm1 = nn.LayerNorm(dim)
        self.norm2 = nn.LayerNorm(dim)
        self.attn = Attention(dim, num_heads)
        if drop_path > 0:
            self.drop_path = DropPath(drop_path)
        else:
            self.drop_path = nn.Identity()
        self.mlp = Mlp(dim, dim * mlp_ratio)
        self.extra_gflops = (num_heads * patch_n * (dim // num_heads) * patch_n * 2) / (1000**3)

    def forward_impl(self, x):
        with torch.cuda.amp.autocast(True):
            x = x + self.drop_path(self.attn(self.norm1(x)))
            x = x + self.drop_path(self.mlp(self.norm2(x)))
        return x

    def forward(self, x):
        if self.using_checkpoint:
            return checkpoint(self.forward_impl, x)
        else:
            return self.forward_impl(x)


class PatchEmbedding(nn.Module):
    def __init__(self, input_size=224, patch_size=32, in_channels: int = 3, dim: int = 768):
        super().__init__()
        if isinstance(input_size, int):
            input_size = (input_size, input_size)
        if isinstance(patch_size, int):
            patch_size = (patch_size, patch_size)
        H = input_size[0] // patch_size[0]
        W = input_size[1] // patch_size[1]
        self.num_patches = H * W
        self.proj = nn.Conv2d(in_channels, dim, kernel_size=patch_size, stride=patch_size)

    def forward(self, x):
        x = self.proj(x).flatten(2).transpose(1, 2)
        return x


def build_model(name="ViT-L/14@336px", using_checkpoint: bool = True):
    if name == "ViT-B/32":
        model = VisionTransformer(
            input_size=224,
            patch_size=32,
            in_channels=3,
            dim=768,
            embedding_size=512,
            depth=12,
            num_heads=12,
            drop_path_rate=0.1,
            using_checkpoint=using_checkpoint,
        )
    elif name == "ViT-B/16":
        model = VisionTransformer(
            input_size=224,
            patch_size=16,
            in_channels=3,
            dim=768,
            embedding_size=768,
            depth=12,
            num_heads=12,
            drop_path_rate=0.1,
            using_checkpoint=using_checkpoint,
        )
    elif name == "ViT-L/14":
        model = VisionTransformer(
            input_size=224,
            patch_size=14,
            in_channels=3,
            dim=1024,
            embedding_size=768,
            depth=24,
            num_heads=16,
            drop_path_rate=0.1,
            using_checkpoint=using_checkpoint,
        )
    elif name == "ViT-L/14@336px":
        model = VisionTransformer(
            input_size=336,
            patch_size=14,
            in_channels=3,
            dim=1024,
            embedding_size=768,
            depth=24,
            num_heads=16,
            drop_path_rate=0.1,
            using_checkpoint=using_checkpoint,
        )
    return model


def _convert_image_to_rgb(image):
    return image.convert("RGB")


def transform(im_size):
    return Compose(
        [
            Resize(im_size, interpolation=InterpolationMode.BICUBIC),
            CenterCrop(im_size),
            _convert_image_to_rgb,
            ToTensor(),
            Normalize((0.48145466, 0.4578275, 0.40821073), (0.26862954, 0.26130258, 0.27577711)),
        ]
    )


def load_model_and_transform(name="ViT-L/14@336px", using_checkpoint: bool = True):
    if name == "ViT-B/32":
        return build_model(name, using_checkpoint=using_checkpoint), transform(224)
    elif name == "ViT-B/16":
        return build_model(name, using_checkpoint=using_checkpoint), transform(224)
    elif name == "ViT-L/14":
        return build_model(name, using_checkpoint=using_checkpoint), transform(224)
    elif name == "ViT-L/14@336px":
        return build_model(name, using_checkpoint=using_checkpoint), transform(336)
    else:
        raise
