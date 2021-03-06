import torch
from torch import nn
from torch.nn import functional as F
from math import sqrt

class Distribution_Model(nn.Module):
    """
    Default LinearNet which has 3 fc layers
    """
    def __init__(self, args, input_size, output_size):
        # input_size : ~1024 * 24, output_size : ~2048
        super().__init__()
        self.fcs = nn.Sequential(
            nn.Linear(input_size, 2048),
            nn.BatchNorm1d(2048),
            nn.ReLU(),
            nn.Dropout(),
            nn.Linear(2048, 2048),
            nn.BatchNorm1d(2048),
            nn.ReLU(),
            nn.Dropout(),
            nn.Linear(2048, 2048),
            nn.BatchNorm1d(2048),
            nn.ReLU(),
            nn.Dropout(),
            nn.Linear(2048, 2048),
            nn.BatchNorm1d(2048),
            nn.ReLU(),
            nn.Dropout(),
            nn.Linear(2048, output_size)
        )

    def forward(self, x):
        return self.fcs(x)