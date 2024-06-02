# Copyright 2024 The YASTN Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
""" Functions performing many CTMRG steps until convergence and return of CTM environment tensors for mxn lattice. """
from typing import NamedTuple
import logging
from yastn.tensor.linalg import svd
import numpy as np

logger = logging.Logger('ctmrg')


class CTMRGout(NamedTuple):
    sweeps : int = 0
    max_dsv : float = None


def calculate_corner_svd(env):
    corner_sv = {}
    for site in env.sites():
        corner_sv[site, 'tl'] = svd(env[site].tl, compute_uv=False)
        corner_sv[site, 'tr'] = svd(env[site].tr, compute_uv=False)
        corner_sv[site, 'bl'] = svd(env[site].bl, compute_uv=False)
        corner_sv[site, 'br'] = svd(env[site].br, compute_uv=False)
    return corner_sv

def diff_compatible(a, b):
    if len(a) > len(b):
        a_ = a
        b_ = np.zeros(len(a))
        b_[0:len(b)] = b
    else:
        b_ = b
        a_ = np.zeros(len(b))
        a_[0:len(a)] = a

    return np.linalg.norm(a_ - b_, ord = np.inf)

def ctmrg_(env, max_sweeps=1, iterator_step=1, method='2site', opts_svd=None, corner_tol=None):
    r"""
    Generator executing ctmrg().
    """
    # init
    max_dsv = None
    truncation_method = None
    if corner_tol:
        old_corner_sv = calculate_corner_svd(env)

    for sweep in range(1, max_sweeps + 1):
        #
        env.update_(method=method, opts_svd=opts_svd)
        if corner_tol:  # check convergence of corners singular values
            if sweep % 2 == 1: continue
            corner_sv = calculate_corner_svd(env)

            if not 'tol_multiplets' in opts_svd.keys():
                dsv = []
                for k, v in corner_sv.items():
                    s_old = np.sort(np.diag((old_corner_sv[k] / old_corner_sv[k].norm(p='inf').item()).to_numpy()))[::-1]
                    s_new = np.sort(np.diag((v / v.norm(p='inf').item()).to_numpy()))[::-1]
                    diff = diff_compatible(s_new, s_old)
                    dsv.append(diff)

                max_dsv = max(dsv)
                truncation_method = "Dense"
            else:
                max_dsv = max((old_corner_sv[k] / old_corner_sv[k].norm(p='inf').item() - v / v.norm(p='inf').item()).norm(p='inf').item() \
                            for k, v in corner_sv.items())

                # for k, v in corner_sv.items():
                #     print(k)
                #     num_of_sv = 0
                #     for t in v.get_legs()[0].t:
                #         try:
                #             print("t, sv", t, (old_corner_sv[k][t, t]).tolist())
                #         except:
                #             pass
                #         print("t, sv", t, (v[t, t]).tolist())
                #         num_of_sv = num_of_sv + len(v[t, t].tolist())
                #     print((old_corner_sv[k] / old_corner_sv[k].norm(p='inf').item() - v / v.norm(p='inf').item()).norm(p='inf').item())
                #     print("Num of SV:", num_of_sv)

                truncation_method = "Symmetric"

            old_corner_sv = corner_sv

            logging.info(f'Sweep = {sweep:03d}  max_d_corner_singular_values = {max_dsv}  Truncation method: {truncation_method}')

            if corner_tol and max_dsv < corner_tol:
                break

        if iterator_step and sweep % iterator_step == 0 and sweep < max_sweeps:
            yield CTMRGout(sweeps=sweep, max_dsv=max_dsv)
    yield CTMRGout(sweeps=sweep, max_dsv=max_dsv)
