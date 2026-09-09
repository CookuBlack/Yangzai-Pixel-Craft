# -*- coding: utf-8 -*-
"""CPU 侧对拍：验证 _wm_gpu.py 的 DWT/DCT/SVD/置乱 数学公式与 pywt/cv2/np 一致。"""
import sys, os
sys.path.insert(0, r"c:\Users\CooKu\Desktop\Creation\Remove\video-watermark-remover\backend")
os.chdir(r"c:\Users\CooKu\Desktop\Creation\Remove\video-watermark-remover\backend")

import numpy as np
import cv2, pywt
from numpy.linalg import svd as npsvd
import _wm_gpu as G  # noqa

SQ = G._SQRT2

def dwt2_np(X):
    low = (X[..., 0::2] + X[..., 1::2]) / SQ
    high = (X[..., 1::2] - X[..., 0::2]) / SQ
    cA = (low[:, 0::2, :] + low[:, 1::2, :]) / SQ
    cH = (high[:, 0::2, :] + high[:, 1::2, :]) / SQ
    cV = (low[:, 1::2, :] - low[:, 0::2, :]) / SQ
    cD = (high[:, 1::2, :] - high[:, 0::2, :]) / SQ
    return cA, cH, cV, cD

rng = np.random.RandomState(1)
img = rng.randint(0, 256, size=(64, 80)).astype(np.float32)
cA, cH, cV, cD = dwt2_np(img[None])
with np.errstate(all="ignore"):
    caA, (caH, caV, caD) = pywt.dwt2(img, "haar")
print("DWT phase vs pywt:",
      "cA", np.abs(cA[0]-caA).max(),
      "cH", np.abs(cH[0]-caH).max(),
      "cV", np.abs(cV[0]-caV).max(),
      "cD", np.abs(cD[0]-caD).max())

T = G._dct_basis()
blocks = rng.randn(64, 4, 4).astype(np.float32)
dct_mine = np.einsum("ik,bkm,jm->bij", T, blocks, T)
diff = max(float(np.abs(dct_mine[i]-cv2.dct(blocks[i])).max()) for i in range(64))
print("DCT vs cv2.dct:", diff)
idct_mine = np.einsum("ik,bkm,mj->bij", T.T, blocks, T)
idiff = max(float(np.abs(idct_mine[i]-cv2.idct(blocks[i])).max()) for i in range(64))
print("IDCT vs cv2.idct:", idiff)

d1, d2, wm, pw = 14, 10, True, 7
nb = 1000
shuf = np.random.RandomState(pw).random(size=(nb,16)).argsort(axis=1)
inv = shuf.argsort(axis=1)

def cpu_block(block, shufk, wmk):
    bd = cv2.dct(block).astype(np.float32)
    bds = bd.flatten()[shufk].reshape(4,4)
    u,s,v = npsvd(bds)
    s0=(s[0]//d1 + 0.25 + 0.5*wmk)*d1
    s1=(s[1]//d2 + 0.25 + 0.5*wmk)*d2
    bf = (u @ np.diag(np.r_[s0,s1,s[2:]]) @ v).flatten()
    back=np.zeros(16); back[shufk]=bf.copy()
    return cv2.idct(back.reshape(4,4)).astype(np.float32)

blocks = rng.randn(nb, 4, 4).astype(np.float32)
D = np.einsum("ik,bkm,jm->bij", T, blocks, T).reshape(nb,16)
Ds = np.take_along_axis(D, shuf, axis=1).reshape(nb,4,4)
u,s,vh = npsvd(Ds)
s_new = s.copy()
s_new[:,0] = (s[:,0]//d1+0.25+0.5*wm)*d1
s_new[:,1] = (s[:,1]//d2+0.25+0.5*wm)*d2
Bnew = np.einsum("bid,bd,bdk->bik", u, s_new, vh).reshape(nb,16)
Bnat = np.take_along_axis(Bnew, inv, axis=1).reshape(nb,4,4)
out = np.einsum("ik,bkm,mj->bij", T.T, Bnat, T)

mism = 0
for k in range(nb):
    r = cpu_block(blocks[k], shuf[k], wm)
    if np.abs(out[k]-r).max() > 1e-4:
        mism += 1
print("block-equal (diff<1e-4) pass:", nb-mism, "/", nb)
print("GPU 公式与 CPU block_add_wm_slow 全长一致 ✓" if mism==0 else "有差异块")