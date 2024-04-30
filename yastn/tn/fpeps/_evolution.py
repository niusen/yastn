"""
Routines for tensor update (tu) timestep on a 2D lattice.
tu supports fermions though application of swap-gates.
PEPS tensors have 5 legs: (top, left, bottom, right, system)
In case of purification, system leg is a fusion of (ancilla, system)
"""
from ... import tensordot, vdot, svd_with_truncation, svd, ncon, eigh_with_truncation
from ._peps import Peps2Layers
from ._gates_auxiliary import apply_gate_onsite, apply_gate_nn, gate_fix_order, apply_bond_tensors
from typing import NamedTuple


class Evolution_out(NamedTuple):
    truncation_error: float = 0
    optimal_pinv_cutoff: float = 0
    min_eigenvalue: float = 0
    second_eigenvalue: float = 0
    fixed_eigenvalues: float = 0


def evolution_step_(env, gates, opts_svd,
                    symmetrize=True, initialization="EAT",
                    pinv_cutoffs=(1e-12, 1e-10, 1e-8, 1e-6), max_iter=1000):
    r"""
    Perform a single step of PEPS evolution by applying a list of gates,
    performing bond-dimension truncation after each application of a two-site gate.

    Parameters
    ----------
    env: EnvNTU | ...
        Environment class containing PEPS state, which is updated in place,
        and a method to calculate bond metric tensors employed during truncation.
    gates: Gates
        The gates that will be applied to PEPS.
    symmetrize : bool
        Whether to iterate through provided gates forward and then backward, resulting in a 2nd order method.
        In that case, each gate should correspond to half of desired timestep.
    opts_svd : dict | Sequence[dict]
        Options passed to :meth:`yastn.linalg.svd_with_truncation` which are used
        to initially truncate the bond dimension, before it is further optimized iteratively.
        In particular, it fixes bond dimensions (potentially, sectorial).
        It is possible to prvide a list of dicts (with decreasing bond dimensions),
        in which case the truncation is done step by step.
    initialization : str
        "SVD" or "EAT". Type of procedure initializing the optimization of truncated PEPS tensors
        after application of two-site gate. Employ plain SVD, or SVD that includes
        a product approximation of bond metric tensor. The default is "EAT".
    pinv_cutoffs : Sequence[float] | float
        List of pseudo-inverse cutoffs. During iterative optimization, use the one that gives the smallest error.
    max_iter : int
        A maximal number of iterative steps for each truncation optimization.

    Returns
    -------
    Evolution_out(NamedTuple)
        Namedtuple containing fields:
            * :code:`truncation_error` for all applied gates, calculated according to metric specified by env.
    """
    infos = []

    psi = env.psi
    if isinstance(psi, Peps2Layers):
        psi = psi.ket  # to make it work with CtmEnv

    for gate in gates.local:
        psi[gate.site] = apply_gate_onsite(psi[gate.site], gate.G)
    for gate in gates.nn:
        infos.append(apply_gate_nn_and_truncate_(env, psi, gate, opts_svd, initialization, pinv_cutoffs, max_iter))
    if symmetrize:
        for gate in gates.nn[::-1]:
            infos.append(apply_gate_nn_and_truncate_(env, psi, gate, opts_svd, initialization, pinv_cutoffs, max_iter))
        for gate in gates.local[::-1]:
            psi[gate.site] = apply_gate_onsite(psi[gate.site], gate.G)

    return Evolution_out(*zip(*infos))


def apply_gate_nn_and_truncate_(env, psi, gate, opts_svd, initialization="EAT",
                   pinv_cutoffs=(1e-12, 1e-10, 1e-8, 1e-6), max_iter=1000):
    r"""
    Applies a nearest-neighbor gate to a PEPS tensor and optimizes the resulting tensor using alternate
    least squares.

    Parameters
    ----------
    env             : class ClusterEnv
    gate            : A Gate object representing the nearest-neighbor gate to apply.
    truncation_mode : str
                    The mode to use for truncation of the environment tensors. Can be
                    'normal' or 'optimal'.
    step            : str
                    The optimization step to perform. Can be 'svd-update', 'one-step', or 'two-step'.
    env_type        : str
                    The type of environment to use for optimization. Can be 'NTU' (neighborhood tensor update),
                        'FU'(full update - to be added).
    opts_svd        : dict, optional
                    A dictionary with options for the SVD truncation. Default is None.

    Returns
    -------
        peps : The optimized PEPS tensor (yastn.fpeps.Lattice).
        info : dict
             A dictionary with information about the optimization. Contains the following keys:
             - 'svd_error': The SVD truncation error.
             - 'tu_error': The truncation error after optimization.
             - 'optimal_cutoff': The optimal cutoff value used for inverse.

    """
    dirn, l_ordered = psi.nn_bond_type(gate.bond)
    f_ordered = psi.f_ordered(gate.bond)
    s0, s1 = gate.bond if l_ordered else gate.bond[::-1]

    G0, G1 = gate_fix_order(gate.G0, gate.G1, l_ordered, f_ordered)
    Q0, Q1, R0, R1, Q0f, Q1f = apply_gate_nn(psi[s0], psi[s1], G0, G1, dirn)

    fgf = env.bond_metric(Q0, Q1, s0, s1, dirn)

    # enforce hermiticity
    fgf = (fgf + fgf.H) / 2

    # check metric tensor eigenvalues
    S, U = fgf.eigh(axes=(0, 1))
    smin, smax = min(S._data), max(S._data)
    try:
        smax2 = max(x for x in S._data if x < smax)
    except ValueError:
        smax2 = 0.
    asmin = abs(smin)

    # fix (smin, -smin) eigenvalues to |smin|
    num_fixed = sum(S._data < asmin).item() / len(S._data)
    S._data[S._data < asmin] = asmin
    fgf_fixed = U @ S @ U.H

    M0, M1, truncation_error2, optimal_pinv_cutoff = truncate_and_optimize(fgf_fixed, R0, R1, initialization, opts_svd, pinv_cutoffs, max_iter)
    psi[s0], psi[s1] = apply_bond_tensors(Q0f, Q1f, M0, M1, dirn)

    return Evolution_out(truncation_error=truncation_error2 ** 0.5,
                         optimal_pinv_cutoff=optimal_pinv_cutoff,
                         min_eigenvalue = smin / smax,
                         second_eigenvalue = smax2 / smax,
                         fixed_eigenvalues = num_fixed)


def truncate_and_optimize(fgf, R0, R1, initialization, opts_svd, pinv_cutoffs, max_iter):
    """
    First we truncate RA and RB tensors based on the input truncation_mode with
    function environment_aided_truncation_step. Then the truncated
    MA and MB tensors are subjected to least square optimization to minimize
     the truncation error with the function tu_single_optimization.
    """
    R01 = R0 @ R1
    R01 = R01.fuse_legs(axes=[(0, 1)])
    gf = fgf.unfuse_legs(axes=0)
    fgR01 = fgf @ R01
    gR01 =  gf @ R01
    gRR = vdot(R01, fgR01).item()

    if isinstance(opts_svd, dict):
        opts_svd = [opts_svd]

    for opts in opts_svd:
        R0, R1, svd_error2 = environment_aided_truncation_step(gRR, fgf, fgR01, R0, R1, initialization, opts, pinv_cutoffs)
        R0, R1, truncation_error2, optimal_pinv_cutoff  = tu_single_optimization(R0, R1, gR01, gf, gRR, svd_error2, pinv_cutoffs, max_iter)
        R0, R1 = truncation_step(R0, R1, opts, normalize=True)

    return R0, R1, truncation_error2, optimal_pinv_cutoff


def truncation_step(RA, RB, opts_svd, normalize=False):
    """ svd truncation of central tensor """

    U, S, V = svd_with_truncation(RA @ RB, sU=RA.s[1], **opts_svd)
    if normalize:
        S = S / S.norm(p='inf')
    S = S.sqrt()
    MA, MB = S.broadcast(U, V, axes=(1, 0))
    return MA, MB


def environment_aided_truncation_step(gRR, fgf, fgRAB, RA, RB, initialization, opts_svd, pinv_cutoffs):

    """
    truncation_mode = 'optimal' is for implementing EAT algorithm only applicable for symmetric
    tensors and offers no advantage for dense tensors.

    Returns
    -------
        MA, MB: truncated pair of tensors before alternate least square optimization
        svd_error2: here just implies the error2 incurred for the initial truncation
               before the optimization
    """

    if initialization == 'EAT':
        g = fgf.unfuse_legs(axes=(0, 1))
        G = ncon((g, RA, RB, RA, RB), ([1, 2, 3, 4], [3, -1], [-3, 4], [1, -2], [-4, 2]), conjs=(0, 0, 0, 1, 1))
        [ul, _, vr] = svd_with_truncation(G, axes=((0, 1), (2, 3)), D_total=1)
        ul = ul.remove_leg(axis=2)
        vr = vr.remove_leg(axis=0)
        GL, GR = ul.transpose(axes=(1, 0)), vr
        _, SL, UL = svd(GL)
        UR, SR, _ = svd(GR)
        XL, XR = SL.sqrt() @ UL, UR @ SR.sqrt()
        XRRX = XL @ XR
        U, L, V = svd_with_truncation(XRRX, sU=RA.get_signature()[1], **opts_svd)
        mA, mB = U @ L.sqrt(), L.sqrt() @ V
        MA, MB, svd_error2, _ = optimal_initial_pinv(mA, mB, RA, RB, gRR, SL, UL, SR, UR, fgf, fgRAB, pinv_cutoffs)
        return MA, MB, svd_error2
    elif initialization == 'SVD':
        MA, MB = truncation_step(RA, RB, opts_svd)
        MAB = MA @ MB
        MAB = MAB.fuse_legs(axes=[(0, 1)])
        gMM = vdot(MAB, fgf @ MAB).item()
        gMR = vdot(MAB, fgRAB).item()
        svd_error2 = abs((gMM + gRR - gMR - gMR.conjugate()) / gRR)

    return MA, MB, svd_error2


def tu_single_optimization(MA, MB, gRAB, gf, gRR, svd_error2, pinv_cutoffs, max_iter):

    """ Optimizes the matrices MA and MB by minimizing the truncation error using least square optimization """

    MA_guess, MB_guess = MA, MB
    truncation_error2_old_A, truncation_error2_old_B = 0, 0
    epsilon1, epsilon2 = 0, 0

    for _ in range(max_iter):
        # fix MB and optimize MA
        j_A = tensordot(gRAB, MB, axes=(1, 1), conj=(0, 1)).fuse_legs(axes=[(0, 1)])
        g_A = tensordot(MB, gf, axes=(1, 1), conj=(1, 0)).fuse_legs(axes=((1, 0), 2))
        g_A = g_A.unfuse_legs(axes=1)
        g_A = tensordot(g_A, MB, axes=(2, 1)).fuse_legs(axes=(0, (1, 2)))
        truncation_error2, optimal_pinv_cutoff, MA = optimal_pinv(g_A, j_A, gRR, pinv_cutoffs)

        epsilon1 = abs(truncation_error2_old_A - truncation_error2)
        truncation_error2_old_A = truncation_error2
        count1, count2 = 0, 0
        if truncation_error2 >= svd_error2:
            MA = MA_guess
            epsilon1, truncation_error2_old_A = 0, 0
            count1 += 1

         # fix MA and optimize MB
        j_B = tensordot(MA, gRAB, axes=(0, 0), conj=(1, 0)).fuse_legs(axes=[(0, 1)])
        g_B = tensordot(MA, gf, axes=(0, 0), conj=(1, 0)).fuse_legs(axes=((0, 1), 2))
        g_B = g_B.unfuse_legs(axes=1)
        g_B = tensordot(g_B, MA, axes=(1, 0)).fuse_legs(axes=(0, (2, 1)))
        truncation_error2, optimal_pinv_cutoff, MB = optimal_pinv(g_B, j_B, gRR, pinv_cutoffs)

        epsilon2 = abs(truncation_error2_old_B - truncation_error2)
        truncation_error2_old_B = truncation_error2

        if truncation_error2 >= svd_error2:
            MB = MB_guess
            epsilon2, truncation_error2_old_B = 0, 0
            count2 += 1

        count = count1 + count2
        if count > 0:
            break

        epsilon = max(epsilon1, epsilon2)
        if epsilon < 1e-13:  # convergence condition
            break

    return MA, MB, truncation_error2, optimal_pinv_cutoff


def optimal_initial_pinv(mA, mB, RA, RB, gRR, SL, UL, SR, UR, fgf, fgRAB, pinv_cutoffs):

    """ function for choosing the optimal initial cutoff for the inverse which gives the least svd_error2 """

    results = []
    for c_off in pinv_cutoffs:
        XL_inv, XR_inv = tensordot(UL.conj(), SL.sqrt().reciprocal(cutoff=c_off), axes=(0, 0)), tensordot(SR.sqrt().reciprocal(cutoff=c_off), UR.conj(), axes=(1, 1))
        pA, pB = XL_inv @ mA, mB @ XR_inv
        MA, MB = RA @ pA, pB @ RB
        MAB = MA @ MB
        MAB = MAB.fuse_legs(axes=[(0, 1)])
        gMM = vdot(MAB, fgf @ MAB).item()
        gMR = vdot(MAB, fgRAB).item()
        svd_error2 = abs((gMM + gRR - gMR - gMR.conjugate()) / gRR)
        results.append((svd_error2, c_off, MA, MB))

    svd_error2, c_off, MA, MB = min(results, key=lambda x: x[0])

    return MA, MB, svd_error2, c_off


def optimal_pinv(gg, J, gRR, pinv_cutoffs):
    """ solve pinv(gg) * J, optimizing pseudoinverse cutoff for tu error2. """

    assert (gg - gg.conj().transpose(axes=(1, 0))).norm() < 1e-12 * gg.norm()
    S, U = eigh_with_truncation(gg, axes=(0, 1), tol=1e-14)
    UdJ = tensordot(J, U, axes=(0, 0), conj=(0, 1))

    results = []
    for c_off in pinv_cutoffs:
        Sd = S.reciprocal(cutoff=c_off)
        SdUdJ = Sd.broadcast(UdJ, axes=0)
        # Mnew = tensordot(SdUdJ, V, axes=(0, 0), conj=(0, 1))
        Mnew = U @ SdUdJ

        # calculation of errors with respect to metric
        met_newA = vdot(Mnew, gg @ Mnew).item()
        met_mixedA = vdot(Mnew, J).item()
        error2 = abs((met_newA + gRR - met_mixedA - met_mixedA.conjugate()) / gRR)
        results.append((error2, c_off, Mnew))

    truncation_error2, optimal_pinv_cutoff, Mnew = min(results, key=lambda x: x[0])

    Mnew = Mnew.unfuse_legs(axes=0)
    return truncation_error2, optimal_pinv_cutoff, Mnew
