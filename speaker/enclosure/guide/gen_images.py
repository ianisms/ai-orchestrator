"""
Generate reference diagrams for the CM5 Smart Speaker enclosure guide.
Output: B:/speaker/enclosure/guide/imgs/*.png
"""
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.patheffects as pe
from matplotlib.patches import FancyArrowPatch, Arc, Circle, Rectangle, FancyBboxPatch, Wedge
from matplotlib.patches import PathPatch
from matplotlib.path import Path
import numpy as np
import os

IMGDIR = r'B:\speaker\enclosure\guide\imgs'
os.makedirs(IMGDIR, exist_ok=True)

# ── Palette ───────────────────────────────────────────────────────────────────
BG       = '#0d1e2e'
TEAL     = '#2d8a94'
TEAL_LT  = '#4aacb8'
TEAL_PALE= '#c8ecf0'
AMBER    = '#e07020'
AMBER_LT = '#f0a060'
SLATE    = '#2a3a4a'
SLATE_LT = '#4a6a7a'
GREEN    = '#2aaa4a'
RED      = '#e03030'
WOOD     = '#b07030'
WOOD_LT  = '#d09050'
GREY     = '#8aacb8'
WHITE    = '#f0f4f6'
GOLD     = '#d0a020'

def save(fig, name):
    p = os.path.join(IMGDIR, name)
    fig.savefig(p, dpi=180, bbox_inches='tight', facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f'  saved {name}')

