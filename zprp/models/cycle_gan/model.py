from typing import Any, Mapping, Sequence

import pytorch_lightning as pl
import torch
import torch.nn as nn
from torch import Tensor, optim
from torchvision.utils import make_grid

from zprp.models.cycle_gan.components import Discriminator, Generator


class LSGANLoss(nn.Module):
    """Mean Square error"""

    @staticmethod
    def forward(preds: Tensor, targets: Tensor) -> Tensor:
        """Compute the MSE on given preds and targets

        Args:
            preds: Predicted outputs
            targets: Ground truth

        Returns:
            MSE value
        """
        return torch.mean((preds - targets) ** 2)


class CycleConsistencyLoss(nn.Module):
    """L1 norm"""

    @staticmethod
    def forward(real: Tensor, reconstructed: Tensor) -> Tensor:
        """Compute the cycle consistency loss on give real and reconstructed images ([N, C, H, W])

        Args:
            real: Real images
            reconstructed: Reconstructed images

        Returns:
            L1 norm value of difference between images
        """
        return torch.mean(torch.abs(real - reconstructed))


class CycleGAN(pl.LightningModule):
    """
    A CycleGAN with adjsutable adversarial loss vs cycle consistency loss ratio.
    https://arxiv.org/pdf/1703.10593
    """

    def __init__(
        self,
        GeneratorClass: type[nn.Module] | None = None,
        DiscriminatorClass: type[nn.Module] | None = None,
        LSGANLossClass: type[nn.Module] | None = None,
        CycleConsistencyLossClass: type[nn.Module] | None = None,
        RegularizationClass: type[nn.Module] | None = None,
        lambda_param: float = 2.0,
        optimizer_kwargs: dict[str, Any] | None = None,
    ) -> None:
        """Init the CycleGan

        Args:
            GeneratorClass: Generator class to use. If None, defaults to a Unet-like model (check .componens).
            DiscriminatorClass: Discriminator class to use. If None, defaults to a CNN with a linear head (check .componens).
            LSGANLossClass: Adversarial loss class. In None, defaults to MSE on preds and targets.
            CycleConsistencyLossClass: Cycle consistency loss class. If None, defaults to the original L1 norm of image difference.
            lambda_param: Factor to scale the cycle loss by. Defaults to 2.0.
            optimizer_kwargs: Kwargs to pass to the Adam optimizers. If None, uses defaults lr=0.0002 and betas=(0.5, 0.999)
        """
        super().__init__()
        self.save_hyperparameters()

        GeneratorClass = GeneratorClass or Generator
        DiscriminatorClass = DiscriminatorClass or Discriminator
        self.g = GeneratorClass()
        self.f = GeneratorClass()
        self.dx = DiscriminatorClass()
        self.dy = DiscriminatorClass()

        self._optimizer_kwargs = optimizer_kwargs or {"lr": 0.0002, "betas": (0.5, 0.999)}

        # required when using multiple optimizers
        # https://lightning.ai/docs/pytorch/stable/common/optimization.html
        self.automatic_optimization = False

        self.lsgan_loss = (LSGANLossClass or LSGANLoss)()
        self.cycle_consistency_loss = (CycleConsistencyLossClass or CycleConsistencyLoss)()

        self.lambda_param = lambda_param

        self.regularization = RegularizationClass() if RegularizationClass else None

    def _discriminator_loss(self, real_preds: Tensor, fake_preds: Tensor) -> Tensor:
        """Adversarial loss on real and fake images ([N, C, H, W])

        Args:
            real_preds: Preds on real images
            fake_preds: Preds on images generated by the model

        Returns:
            Adversarial loss valuex
        """
        real_loss = self.lsgan_loss(real_preds, torch.ones_like(real_preds))
        fake_loss = self.lsgan_loss(fake_preds, torch.zeros_like(fake_preds))
        return (real_loss + fake_loss) * 0.5  # type: ignore[no-any-return]

    def _cycle_loss(self, real_x: Tensor, cycle_x: Tensor, real_y: Tensor, cycle_y: Tensor) -> Tensor:
        """Cycle consistency loss on both X->Y and Y->X preds staled by the lambda factor

        Args:
            real_x: Real images from domain X
            cycle_x: Recreated images from domain X
            real_y: Real images from domain Y
            cycle_y: Recreated images from domain Y

        Returns:
            Cycle consistency loss
        """
        cycle_x_loss = self.cycle_consistency_loss(real_x, cycle_x)
        cycle_y_loss = self.cycle_consistency_loss(real_y, cycle_y)
        return (cycle_x_loss + cycle_y_loss) * self.lambda_param  # type: ignore[no-any-return]

    def configure_optimizers(
        self,
    ) -> tuple[Sequence[torch.optim.Optimizer], Sequence[Any]]:
        """Create optimizers for both generator and discriminator pairs.
        This function should only be called by the pytorch_lighting framework.

        Returns:
            A sequence of g_optimizer, f_optimizer, dx_optimizer, dy_optimizer plus a sequence of LR schedulers (empty)
        """
        g_optimizer = optim.Adam(self.g.parameters(), **self._optimizer_kwargs)
        f_optimizer = optim.Adam(self.f.parameters(), **self._optimizer_kwargs)
        dx_optimizer = optim.Adam(self.dx.parameters(), **self._optimizer_kwargs)
        dy_optimizer = optim.Adam(self.dy.parameters(), **self._optimizer_kwargs)
        return [g_optimizer, f_optimizer, dx_optimizer, dy_optimizer], []

    def training_step(self, batch: Tensor, batch_idx: int) -> Tensor | Mapping[str, Any] | None:
        """Perform one training step - perform X->Y, Y->X, X->Y->X, Y->X->Y transfers, compute and log loss values.
        This function should only be called by the pytorch_lighting framework.
        """
        real_x, real_y = batch
        g_optimizer, f_optimizer, dx_optimizer, dy_optimizer = self.optimizers()  # type: ignore[attr-defined]

        # 1. Train discriminators

        dx_optimizer.zero_grad()
        dy_optimizer.zero_grad()

        fake_y = self.g(real_x)
        fake_x = self.f(real_y)

        dx_loss = self._discriminator_loss(real_preds=self.dx(real_x), fake_preds=self.dx(fake_x))
        dy_loss = self._discriminator_loss(real_preds=self.dy(real_y), fake_preds=self.dx(fake_y))

        self.manual_backward(dx_loss)
        self.manual_backward(dy_loss)
        dx_optimizer.step()
        dy_optimizer.step()

        # 2. Train generators

        g_optimizer.zero_grad()
        f_optimizer.zero_grad()

        fake_y = self.g(real_x)
        fake_x = self.f(real_y)

        # adversarial losses of the generators max log(D(G(x))) => min (1 - D(G(x)))^2
        g_loss = self.lsgan_loss(self.dy(fake_y), torch.ones_like(self.dy(fake_y)))
        f_loss = self.lsgan_loss(self.dx(fake_x), torch.ones_like(self.dx(fake_x)))

        cycle_x = self.f(fake_y)
        cycle_y = self.g(fake_x)

        # consistency loss - l1 norm between real and reconstructed images - min l1 error
        total_cycle_loss = self._cycle_loss(real_x, cycle_x, real_y, cycle_y) + (
            self.regularization(real_x, fake_y) if self.regularization else 0
        )

        # backpropagate generator losses sum adversarial and cycle loss
        total_G_loss = g_loss + total_cycle_loss
        total_F_loss = f_loss + total_cycle_loss

        self.manual_backward(
            total_G_loss, retain_graph=True
        )  # otherwise cannot backward pass total_F_loss -> reuse total_cycle loss
        self.manual_backward(total_F_loss)
        g_optimizer.step()
        f_optimizer.step()

        self.log_dict(
            {
                "cyclegan_dx_loss": dx_loss,
                "cyclegan_dy_loss": dy_loss,
                "cyclegan_g_loss": g_loss,
                "cyclegan_f_loss": f_loss,
                "cyclegan_total_cycle_loss": total_cycle_loss,
                "cyclegan_total_g_loss": total_G_loss,
                "cyclegan_total_f_loss": total_F_loss,
            },
            prog_bar=True,
        )

        return None

    def validation_step(self, batch: Tensor, batch_idx: int) -> None:
        """If logger is configured, perform style transfer on given images and log results.
        This function should only be called by the pytorch_lighting framework.
        """
        if not self.logger:
            return

        with torch.no_grad():
            real_x, real_y = batch

            fake_y = self.x_to_y(real_x)
            cycle_x = self.y_to_x(fake_y)
            fake_x = self.y_to_x(real_y)
            cycle_y = self.x_to_y(fake_x)

        for image, name in (
            (fake_y, "cyclegan_fake_y"),
            (fake_x, "cyclegan_fake_x"),
            (cycle_x, "cyclegan_cycle_x"),
            (cycle_y, "cyclegan_cycle_y"),
        ):
            self.logger.experiment.add_image(name, make_grid(self.unnormalize(image)), self.current_epoch)

    def x_to_y(self, x: Tensor) -> Tensor:
        """Transfer images ([N, C, H, W]) from domain X to Y

        Args:
            x: Images from domain X

        Returns:
            Images transferred to domain Y
        """
        return self.g(x)

    def y_to_x(self, y: Tensor) -> Tensor:
        """Transfer images ([N, C, H, W]) from domain Y to X

        Args:
            y: Images from domain y

        Returns:
            Images transferred to domain X
        """
        return self.f(y)

    @staticmethod
    def unnormalize(x: Tensor) -> Tensor:
        """Scale a tensor from value range [-1, 1] to [0, 1]

        Args:
            x: Normalized float tensor

        Returns:
            Denormalized float tensor
        """
        return (x * 0.5) + 0.5
