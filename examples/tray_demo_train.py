# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""A small, slow "training" to see progress in `colab job status` and the tray.

Logistic regression on synthetic data with numpy (CPU, no downloads),
slowed down so the progress bar, the epochs and the falling loss can be
watched. It reports what the tray shows:

* a tqdm bar per epoch (the card's progress inside the epoch),
* ``report(epoch=, epochs=, loss=, acc=)`` after each epoch (overall
  progress, "2 / 5 epoch", and the loss with its trend).

Run it as a background job, e.g. with the demo profile in
examples/profiles.example.toml:

    colab profile run demo-train
    colab tray            # or: colab job status -s <session> --json

or directly: ``colab run --detach examples/tray_demo_train.py --epochs 5``.
Outside a job, ``report`` does nothing, so it also runs locally.
"""

import argparse
import time

import numpy as np

try:
    from colab_feedback import report
except ImportError:  # not in a `colab job`
    def report(**metrics):
        pass

try:
    from tqdm import tqdm
except ImportError:
    def tqdm(iterable, **kwargs):
        return iterable


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--epochs", type=int, default=5)
    parser.add_argument("--steps", type=int, default=60, help="Steps per epoch.")
    parser.add_argument("--delay", type=float, default=1.0, help="Seconds per step (to watch it).")
    parser.add_argument("--lr", type=float, default=0.5)
    args = parser.parse_args()

    rng = np.random.default_rng(0)
    n, d = 4096, 20
    x = rng.normal(size=(n, d))
    true_w = rng.normal(size=d)
    y = (x @ true_w + 0.5 * rng.normal(size=n) > 0).astype(float)
    w = np.zeros(d)

    def loss_and_acc():
        p = 1 / (1 + np.exp(-(x @ w)))
        loss = -np.mean(y * np.log(p + 1e-9) + (1 - y) * np.log(1 - p + 1e-9))
        return float(loss), float(np.mean((p > 0.5) == y))

    print(f"Training on {n} samples, {args.epochs} epochs x {args.steps} steps", flush=True)
    for epoch in range(1, args.epochs + 1):
        for _ in tqdm(range(args.steps), desc=f"epoch {epoch}/{args.epochs}", mininterval=0.5):
            batch = rng.integers(0, n, size=64)
            p = 1 / (1 + np.exp(-(x[batch] @ w)))
            w -= args.lr * x[batch].T @ (p - y[batch]) / len(batch)
            time.sleep(args.delay)
        loss, acc = loss_and_acc()
        report(epoch=epoch, epochs=args.epochs, loss=round(loss, 4), acc=round(acc, 4))
        print(f"epoch {epoch}: loss={loss:.4f} acc={acc:.3f}", flush=True)
    print("done", flush=True)


if __name__ == "__main__":
    main()
