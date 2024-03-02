""" Environments for the <mps| mpo |mps> and <mps|mps>  contractions. """
from __future__ import annotations
from ... import eye, tensordot, ncon, vdot, YastnError, Tensor, qr, svd, ones
from typing import Sequence, Dict, Optional
from . import MpsMpoOBC, MpoPBC
import abc


class MpsProjector():
    # holds reference to a set of n states and
    # owns a set of n mps-mps environments for projections

    def __init__(self, bra, project:Optional[Sequence[MpsMpoOBC]] = None):
        # environments, indexed by bonds with respect to k-th MPS-based projector
        if project and len(project) > 0:
            assert all([bra.N == _mps.N for _mps in project]), "all MPO operators and state should have the same number of sites"
        self.N = bra.N
        self.nr_phys = bra.nr_phys
        self.bra = bra
        self.config = bra.config
        self.ort = [] if project is None else project
        self.Fort = [{} for _ in range(len(self.ort))]

        # initialize environments with respect to orthogonal projections
        for ii in range(len(self.ort)):
            legs = [self.ort[ii].virtual_leg('first'), self.bra.virtual_leg('first').conj()]
            self.Fort[ii][(-1, 0)] = eye(self.config, legs=legs, isdiag=False)
            legs = [self.bra.virtual_leg('last').conj(), self.ort[ii].virtual_leg('last')]
            self.Fort[ii][(self.N, self.N - 1)] = eye(self.config, legs=legs, isdiag=False)
        self._temp = {'Aort': [],}

    def _update_env(self,n,to='last'):
        for ii in range(len(self.ort)):
            _update2_(n, self.Fort[ii], self.bra, self.ort[ii], to, self.nr_phys)

    # methods handling projection wrt. set of states (MPS)
    def update_Aort_(self, n):
        """ Update projection of states to be subtracted from psi. """
        Aort = []
        inds = ((-0, 1), (1, -1, 2), (2, -2)) if self.nr_phys == 1 else ((-0, 1), (1, -1, 2, -3), (2, -2))
        for ii in range(len(self.ort)):
            Aort.append(ncon([self.Fort[ii][(n - 1, n)], self.ort[ii][n], self.Fort[ii][(n + 1, n)]], inds))
        self._temp['Aort'] = Aort

    def update_AAort_(self, bd):
        """ Update projection of states to be subtracted from psi. """
        AAort = []
        nl, nr = bd
        inds = ((-0, 1), (1, -1, -2,  2), (2, -3)) if self.nr_phys == 1 else ((-0, 1), (1, -1, 2, -3), (2, -2))
        for ii in range(len(self.ort)):
            AA = self.ort[ii].merge_two_sites(bd)
            AAort.append(ncon([self.Fort[ii][(nl - 1, nl)], AA, self.Fort[ii][(nr + 1, nr)]], inds))
        self._temp['Aort'] = AAort

    def _project_ort(self, A):
        for ii in range(len(self.ort)):
            x = vdot(self._temp['Aort'][ii], A)
            A = A.apxb(self._temp['Aort'][ii], -x)
        return A


class _EnvParent(metaclass=abc.ABCMeta):

    def __init__(self, bra=None, project=None) -> None:
        """
        Interface for environments of 1D TNs. In particular of the form,

            <bra| (sum_i op_i |ket_i>)

        where op_i can be None/identity.
        """
        self.config = bra.config
        self.bra = bra
        self.N = self.bra.N
        self.nr_phys = bra.nr_phys
        self.F = {}  # dict of envs dict[tuple[int, int], yastn.Tensor]
        self.projector = None if project is None else MpsProjector(bra, project)

    def setup_(self, to='last'):
        r"""
        Setup all environments in the direction given by ``to``.

        Parameters
        ----------
        to: str
            'first' or 'last'.
        """
        for n in self.ket.sweep(to=to):
            self.update_env_(n, to=to)
        return self

    @abc.abstractmethod
    def clear_site_(self, *args):
        r"""
        Clear environments pointing from sites which indices are provided in args.
        """

    @abc.abstractmethod
    def factor(self) -> number:
        r"""
        Collect factors from constituent MPSs and MPOs.
        """

    @abc.abstractmethod
    def measure(self, bd=None) -> number:
        r"""
        Calculate overlap between environments at bd bond.

        Parameters
        ----------
        bd: tuple
            index of bond at which to calculate overlap.
        """

    @abc.abstractmethod
    def update_env_(self, n, to='last'):
        r"""
        Update environment including site n, in the direction given by to.

        Parameters
        ----------
        n: int
            index of site to include to the environment

        to: str
            'first' or 'last'.
        """

    @abc.abstractmethod
    def Heff0(self, C, bd) -> yastn.Tensor:
        r"""
        Action of Heff on central block, Heff0 @ C

        Parameters
        ----------
        C: tensor
            a central block
        bd: tuple
            index of bond on which it acts, e.g. (1, 2) [or (2, 1) -- it is ordered]
        """

    @abc.abstractmethod
    def Heff1(self, A, n) -> yastn.Tensor:
        r"""
        Action of Heff on a single site MPS tensor, Heff1 @ A

        Parameters
        ----------
        A: tensor
            site tensor

        n: int
            index of corresponding site
        """

    @abc.abstractmethod
    def Heff2(self, AA, bd) -> yastn.Tensor:
        r"""
        Action of Heff on central block, Heff2 @ AA.

        Parameters
        ----------
        AA: tensor
            merged tensor for 2 sites.
            Physical legs should be fused turning it effectivly into 1-site update.
        bd: tuple
            index of bond on which it acts, e.g. (1, 2) [or (2, 1) -- it is ordered]
        """

    # functions facilitating projection, if projector is present
    def update_Aort_(self,n:int):
        if self.projector is not None:
            self.projector.update_Aort_(n)

    def update_AAort_(self,bd:Sequence[int]):
        if self.projector is not None:
            self.projector.update_AAort_(bd)

    def _project_ort(self,A):
        if self.projector is None:
            return A
        return self.projector._project_ort(A)


class Env2(_EnvParent):
    # The class combines environments of mps+mps for calculation of expectation values, overlaps, etc.

    def __init__(self, bra=None, ket=None):
        super().__init__(bra)
        self.ket = ket
        if self.bra.nr_phys != self.ket.nr_phys:
            raise YastnError('MpsMpoOBC for bra and ket should have the same number of physical legs.')
        if self.bra.N != self.ket.N:
            raise YastnError('MpsMpoOBC for bra and ket should have the same number of sites.')
        # left boundary
        legs = [self.bra.virtual_leg('first'), self.ket.virtual_leg('first').conj()]
        self.F[(-1, 0)] = eye(self.config, legs=legs, isdiag=False)
        # right boundary
        legs = [self.ket.virtual_leg('last').conj(), self.bra.virtual_leg('last')]
        self.F[(self.N, self.N - 1)] = eye(self.config, legs=legs, isdiag=False)

    def clear_site_(self, *args):
        return _clear_site_(self.F, *args)

    def factor(self):
        return self.bra.factor * self.ket.factor

    def measure(self, bd=(-1, 0)):
        tmp = tensordot(self.F[bd], self.F[bd[::-1]], axes=((0, 1), (1, 0)))
        return self.factor() * tmp.to_number()

    def update_env_(self, n, to='last'):
        _update2_(n, self.F, self.bra, self.ket, to, self.nr_phys)
        if self.projector:
            self.projector._update_env(n,to)

    def Heff0(self, C, bd):
        pass

    def Heff1(self, x, n):
        inds = ((-0, 1), (1, -1, 2), (2, -2)) if self.nr_phys == 1 else ((-0, 1), (1, -1, 2, -3), (2, -2))
        return ncon([self.F[(n - 1, n)], x, self.F[(n + 1, n)]], inds)

    def Heff2(self, AA, bd):
        """ Heff2 @ AA """
        n1, n2 = bd
        axes = (0, (1, 2), 3) if AA.ndim == 4 else (0, (1, 2, 3, 5), 4)
        temp = AA.fuse_legs(axes=axes)
        temp = self.F[(n1 - 1, n1)] @ temp @ self.F[(n2 + 1, n2)]
        temp = temp.unfuse_legs(axes=1)
        if temp.ndim == 6:
            temp = temp.transpose(axes=(0, 1, 2, 3, 5, 4))
        return temp

    def update_env_op_(self, n, op, to='first'):
        """
        Contractions for 2-layer environment update, with on-site operator ``op`` applied on site ``n``.
        """
        if to == 'first':
            temp = tensordot(self.ket[n], self.F[(n + 1, n)], axes=(2, 0))
            op = op.add_leg(axis=0, s=1)
            temp = tensordot(op, temp, axes=(2, 1))
            temp = temp.swap_gate(axes=(0, 2))
            temp = temp.remove_leg(axis=0)
            axes = ((0, 2), (1, 2)) if self.nr_phys == 1 else ((0, 3, 2), (1, 2, 3))
            self.F[(n, n - 1)] = tensordot(temp, self.bra[n].conj(), axes=axes)
        else:  # to == 'last'
            op = op.add_leg(axis=0, s=1)
            temp = tensordot(op, self.ket[n], axes=((2, 1)))
            temp = temp.swap_gate(axes=(0, 2))
            temp = temp.remove_leg(axis=0)
            temp = tensordot(self.F[(n - 1, n)], temp, axes=((1, 1)))
            axes = ((0, 1), (0, 1)) if self.nr_phys == 1 else ((0, 1, 3), (0, 1, 3))
            self.F[(n, n + 1)] = tensordot(self.bra[n].conj(), temp, axes=axes)




class _EnvParent_3(_EnvParent):

    def __init__(self, bra=None, op: Optional[MpsMpoOBC] = None, ket=None, project=None):
        super().__init__(bra, project)
        if not op.N == self.N:
            raise YastnError("MPO operator and state should have the same number of sites")
        self.ket = ket
        self.op = op

        # left boundary
        # legs = [self.bra.virtual_leg('first'), self.ket.virtual_leg('first').conj()]
        # tmp = eye(self.config, legs=legs, isdiag=False)
        # self.F[(-1, 0)] = tmp.add_leg(axis=1, s=-op.virtual_leg('first').s)

        # right boundary
        # legs = [self.ket.virtual_leg('last').conj(), self.bra.virtual_leg('last')]
        # tmp = eye(self.config, legs=legs, isdiag=False)
        # self.F[(self.N, self.N - 1)] = tmp.add_leg(axis=1, s=-op.virtual_leg('last').s)

        legs = [self.bra.virtual_leg('first'), op.virtual_leg('first').conj(), self.ket.virtual_leg('first').conj()]
        self.F[(-1, 0)] = ones(self.config, legs=legs, isdiag=False)

        # right boundary
        legs = [self.ket.virtual_leg('last').conj(), op.virtual_leg('last').conj(), self.bra.virtual_leg('last')]
        self.F[(self.N, self.N - 1)] = ones(self.config, legs=legs, isdiag=False)

    def clear_site_(self, *args):
        return _clear_site_(self.F, *args)

    def factor(self) -> number:
        return self.bra.factor * self.op.factor * self.ket.factor

    def measure(self, bd=(-1, 0)):
        tmp = tensordot(self.F[bd], self.F[bd[::-1]], axes=((0, 1, 2), (2, 1, 0)))
        return self.factor() * tmp.to_number()

    def update_env_(self, n, to='last'):
        pass

    def Heff0(self, C, bd):
        bd, ibd = (bd[::-1], bd) if bd[1] < bd[0] else (bd, bd[::-1])
        C = self.op.factor * C
        tmp = self.F[bd].tensordot(C, axes=(2, 0))
        return tmp.tensordot(self.F[ibd], axes=((1, 2), (1, 0)))

    def Heff1(self, A, n):
        pass

    def Heff2(self, AA, bd):
        pass

    def enlarge_bond(self, bd, opts_svd):
        if bd[0] < 0 or bd[1] >= self.N:  # do not enlarge bond outside of the chain
            return False
        AL = self.ket[bd[0]]
        AR = self.ket[bd[1]]
        if (self.op[bd[0]].get_legs(axes=1).t != AL.get_legs(axes=1).t) or \
           (self.op[bd[1]].get_legs(axes=1).t != AR.get_legs(axes=1).t):
            return True  # true if some charges are missing on physical legs of psi

        AL = AL.fuse_legs(axes=((0, 1), 2))
        AR = AR.fuse_legs(axes=(0, (1, 2)))
        shapeL = AL.get_shape()
        shapeR = AR.get_shape()
        if shapeL[0] == shapeL[1] or shapeR[0] == shapeR[1] or \
           ('D_total' in opts_svd and shapeL[1] >= opts_svd['D_total']):
            return False  # maximal bond dimension
        if 'tol' in opts_svd:
            _, R0 = qr(AL, axes=(0, 1), sQ=1)
            _, R1 = qr(AR, axes=(1, 0), Raxis=1, sQ=-1)
            S = svd(R0 @ R1, compute_uv=False)
            if any(S[t][-1] > opts_svd['tol'] * 1.1 for t in S.struct.t):
                return True
        return False


class _Env_mps_mpo_mps(_EnvParent_3):

    def update_env_(self, n, to='last'):
        if to == 'last':
            tmp = ncon([self.bra[n].conj(), self.F[(n - 1, n)]], ((1, -1, -0), (1, -2, -3)))
            tmp = self.op[n]._attach_01(tmp)
            self.F[(n, n + 1)] = ncon([tmp, self.ket[n]], ((-0, -1, 1, 2), (1, 2, -2)))
        elif to == 'first':
            tmp = self.ket[n] @ self.F[(n + 1, n)]
            tmp = self.op[n]._attach_23(tmp)
            self.F[(n, n - 1)] = ncon([tmp, self.bra[n].conj()], ((-0, -1, 1, 2), (-2, 2, 1)))
        if self.projector:
            self.projector._update_env(n, to)

    def Heff1(self, A, n):
        nl, nr = n - 1, n + 1
        tmp = self._project_ort(A)
        tmp = tmp @ self.F[(nr, n)]
        tmp = self.op[n]._attach_23(tmp)
        tmp = ncon([self.F[(nl, n)], tmp], ((-0, 1, 2), (2, 1, -2, -1)))
        return self.op.factor * self._project_ort(tmp)

    def Heff2(self, AA, bd):
        n1, n2 = bd if bd[0] < bd[1] else bd[::-1]
        bd, nl, nr = (n1, n2), n1 - 1, n2 + 1
        tmp = self._project_ort(AA)
        tmp = tmp.fuse_legs(axes=((0, 1), 2, 3))
        tmp = tmp @ self.F[(nr, n2)]
        tmp = self.op[n2]._attach_23(tmp)
        tmp = tmp.fuse_legs(axes=(0, 1, (3, 2)))
        tmp = tmp.unfuse_legs(axes=0)
        tmp = self.op[n1]._attach_23(tmp)
        tmp = ncon([self.F[(nl, n1)], tmp], ((-0, 1, 2), (2, 1, -2, -1)))
        tmp = tmp.unfuse_legs(axes=2)
        return self.op.factor * self._project_ort(tmp)


class _Env_mpo_mpo_mpo(_EnvParent_3):

    def update_env_(self, n, to='last'):
        if to == 'last':
            bA = self.bra[n].fuse_legs(axes=(0, 1, (2, 3)))
            tmp = ncon([bA.conj(), self.F[(n - 1, n)]], ((1, -1, -0), (1, -2, -3)))
            tmp = self.op[n]._attach_01(tmp)
            tmp = tmp.unfuse_legs(axes=0)
            self.F[(n, n + 1)] = ncon([tmp, self.ket[n]], ((-0, 3, -1, 1, 2), (1, 2, -2, 3)))
        elif to == 'first':
            kA = self.ket[n].fuse_legs(axes=((0, 3), 1, 2))
            tmp = ncon([kA, self.F[(n + 1, n)]], ((-0, -1, 1), (1, -2, -3)))
            tmp = self.op[n]._attach_23(tmp)
            tmp = tmp.unfuse_legs(axes=0)
            self.F[(n, n - 1)] = ncon([tmp, self.bra[n].conj()], ((-0, 3, -1, 1, 2), (-2, 2, 1, 3)))
        if self.projector:
            self.projector._update_env(n,to)

    def Heff1(self, A, n):
        nl, nr = n - 1, n + 1
        tmp = A.fuse_legs(axes=((0, 3), 1, 2))
        tmp = tmp @ self.F[(nr, n)]
        tmp = self.op[n]._attach_23(tmp)
        tmp = tmp.unfuse_legs(axes=0)
        tmp = ncon([self.F[(nl, n)], tmp], ((-0, 1, 2), (2, -3, 1, -2, -1)))
        return self.op.factor * self._project_ort(tmp)

    def Heff2(self, AA, bd):
        n1, n2 = bd if bd[0] < bd[1] else bd[::-1]
        bd, nl, nr = (n1, n2), n1 - 1, n2 + 1
        tmp = AA.fuse_legs(axes=((0, 2, 5), 1, 3, 4))
        tmp = tmp.fuse_legs(axes=((0, 1), 2, 3))
        tmp = tmp @ self.F[(nr, n2)]
        tmp = self.op[n2]._attach_23(tmp)
        tmp = tmp.fuse_legs(axes=(0, 1, (3, 2)))
        tmp = tmp.unfuse_legs(axes=0)
        tmp = self.op[n1]._attach_23(tmp)
        tmp = tmp.unfuse_legs(axes=0)
        tmp = ncon([self.F[(nl, n1)], tmp], ((-0, 1, 2), (2, -2, -4, 1, -3, -1)))
        tmp = tmp.unfuse_legs(axes=3)
        return self.op.factor * self._project_ort(tmp)


class _Env_mpo_mpo_mpo_aux(_EnvParent_3):

    def update_env_(self, n, to='last'):
        if to == 'last':
            tmp = ncon([self.ket[n], self.F[(n - 1, n)]], ((1, -4, -0, -1), (-3, -2, 1)))
            tmp = tmp.fuse_legs(axes=(0, 1, 2, (3, 4)))
            tmp = self.op[n]._attach_01(tmp)
            bA = self.bra[n].fuse_legs(axes=((0, 1), 2, 3))
            self.F[(n, n + 1)] = ncon([bA.conj(), tmp], ((1, -0, 2), (-2, -1, 1, 2)))
        elif to == 'first':
            bA = self.bra[n].fuse_legs(axes=((0, 1), 2, 3))
            tmp = ncon([bA.conj(), self.F[(n + 1, n)]], ((-0, 1, -1), (-3, -2, 1)))
            tmp = self.op[n]._attach_23(tmp)
            tmp = tmp.unfuse_legs(axes=0)
            self.F[(n, n - 1)] = ncon([self.ket[n], tmp], ((-0, 1, 2, 3), (-2, 1, -1, 2, 3)))
        if self.projector:
            self.projector._update_env(n,to)

    def Heff1(self, A, n):
        nl, nr = n - 1, n + 1
        tmp = A.fuse_legs(axes=(0, (1, 2), 3))
        tmp = ncon([tmp, self.F[(nl, n)]], ((1, -0, -1), (-3, -2, 1)))
        tmp = self.op[n]._attach_01(tmp)
        tmp = tmp.unfuse_legs(axes=0)
        tmp = ncon([tmp, self.F[(nr, n)]], ((-1, 1, -0, 2, -3), (1, 2, -2)))
        return self.op.factor * self._project_ort(tmp)

    def Heff2(self, AA, bd):
        n1, n2 = bd if bd[0] < bd[1] else bd[::-1]
        bd, nl, nr = (n1, n2), n1 - 1, n2 + 1
        tmp = AA.fuse_legs(axes=(0, 2, (1, 3, 4), 5))
        tmp = tmp.fuse_legs(axes=(0, 1, (2, 3)))
        tmp = ncon([tmp, self.F[(nl, n1)]], ((1, -1, -0), (-3, -2, 1)))
        tmp = self.op[n1]._attach_01(tmp)
        tmp = tmp.fuse_legs(axes=(0, 1, (2, 3)))
        tmp = tmp.unfuse_legs(axes=0)
        tmp = self.op[n2]._attach_01(tmp)
        tmp = tmp.unfuse_legs(axes=0)
        tmp = ncon([tmp, self.F[(nr, n2)]], ((-1, -2, 1, 2, -0, -4), (1, 2, -3)))
        tmp = tmp.unfuse_legs(axes=0).transpose(axes=(0, 2, 1, 3, 4, 5))
        return self.op.factor * self._project_ort(tmp)


def _clear_site_(F, *args):
    for n in args:
        F.pop((n, n - 1), None)
        F.pop((n, n + 1), None)

def _update2_(n, F : Dict[tuple[int, int],Tensor], bra : MpsMpoOBC, ket : MpsMpoOBC, to, nr_phys):
    if to == 'first':
        inds = ((-0, 2, 1), (1, 3), (-1, 2, 3)) if nr_phys == 1 else ((-0, 2, 1, 4), (1, 3), (-1, 2, 3, 4))
        F[(n, n - 1)] = ncon([ket[n], F[(n + 1, n)], bra[n].conj()], inds)
    elif to == 'last':
        inds = ((2, 3, -0), (2, 1), (1, 3, -1)) if nr_phys == 1 else ((2, 3, -0, 4), (2, 1), (1, 3, -1, 4))
        F[(n, n + 1)] = ncon([bra[n].conj(), F[(n - 1, n)], ket[n]], inds)



class Env3_pbc(_EnvParent):
    def __init__(self, bra=None, op=None, ket=None, project=None):
        super().__init__(bra, ket, project)
        if self.op.N != self.N:
            raise YastnError('MPO operator and state should have the same number of sites.')
        self.op = op

        # left boundary
        lfb = self.bra.virtual_leg('first')
        lfo = self.op.virtual_leg('first')
        lfk = self.ket.virtual_leg('first')
        tmp_oo = eye(self.config, legs=lfo.conj(), isdiag=False)
        tmp_bk = eye(self.config, legs=[lfb, lfk.conj()], isdiag=False)
        self.F[(-1, 0)] = ncon([tmp_oo, tmp_bk], ((-1, -2), (-0, -3)))

        # right boundary
        llk = self.ket.virtual_leg('last')
        llo = self.op.virtual_leg('last')
        llb = self.bra.virtual_leg('last')
        tmp_oo = eye(self.config, legs=llo.conj(), isdiag=False)
        tmp_bk = eye(self.config, legs=[llk.conj(), llb], isdiag=False)
        self.F[(self.N, self.N - 1)] = ncon([tmp_oo, tmp_bk], ((-1, -2), (-0, -3)))

    def factor(self):
        return self.bra.factor * self.op.factor * self.ket.factor

    def Heff0(self, C, bd):
        bd, ibd = (bd[::-1], bd) if bd[1] < bd[0] else (bd, bd[::-1])
        C = self.op.factor * C
        tmp = self.F[bd].tensordot(C, axes=(3, 0))
        return tmp.tensordot(self.F[ibd], axes=((3, 1, 2), (0, 1, 2)))

    def Heff1(self, A, n):
        nl, nr = n - 1, n + 1
        tmp = self._project_ort(A)
        if self.nr_phys == 1:
            Fr = self.F[(nr, n)].fuse_legs(axes=(0, 1, (2, 3)))
            tmp = tmp.tensordot(Fr, axes=(2, 0))
            tmp = self.op[n]._attach_23(tmp)
            tmp = tmp.unfuse_legs(axes=2)
            tmp = self.F[(nl, n)].tensordot(tmp, axes=((3, 1, 2), (0, 1, 2)))
            tmp = tmp.transpose(axes=(0, 2, 1))
        # elif self.nr_phys == 2 and not self.on_aux:
        #     tmp = tmp.fuse_legs(axes=((0, 3), 1, 2))
        #     Fr = self.F[(nr, n)].fuse_legs(axes=(0, 1, (2, 3)))
        #     tmp = tmp.tensordot(Fr, axes=(2, 0))
        #     tmp = self.op[n]._attach_23(tmp)
        #     tmp = tmp.unfuse_legs(axes=(0, 2))
        #     tmp = tmp.swap_gate(axes=(1, 3))
        #     tmp = self.F[(nl, n)].tensordot(tmp, axes=((1, 2, 3), (2, 3, 0)))
        #     tmp = tmp.transpose(axes=(0, 3, 2, 1))
        # else:  # if self.nr_phys == 2 and self.on_aux:    #todo
        #     tmp = tmp.fuse_legs(axes=(0, (1, 2), 3))
        #     tmp = ncon([tmp, self.F[(nl, n)]], ((1, -0, -1), (-3, -2, 1)))
        #     tmp = self.op[n]._attach_01(tmp)
        #     tmp = tmp.unfuse_legs(axes=0)
        #     tmp = ncon([tmp, self.F[(nr, n)]], ((-1, 1, -0, 2, -3), (1, 2, -2)))
        return self.op.factor * self._project_ort(tmp)


    def Heff2(self, AA, bd):
        n1, n2 = bd if bd[0] < bd[1] else bd[::-1]
        bd, nl, nr = (n1, n2), n1 - 1, n2 + 1

        tmp = self._project_ort(AA)
        if self.nr_phys == 1:
            Fr = self.F[(nr, n2)].fuse_legs(axes=(0, 1, (2, 3)))
            tmp = tmp.fuse_legs(axes=((0, 1), 2, 3))
            tmp = tmp.tensordot(Fr, axes=(2, 0))
            tmp = self.op[n2]._attach_23(tmp)
            tmp = tmp.fuse_legs(axes=(0, 1, (2, 3)))
            tmp = tmp.unfuse_legs(axes=0)
            tmp = self.op[n1]._attach_23(tmp)
            tmp = tmp.unfuse_legs(axes=2)
            tmp = tmp.unfuse_legs(axes=2)
            tmp = self.F[(nl, n1)].tensordot(tmp, axes=((3, 1, 2), (0, 1, 2)))
            tmp = tmp.transpose(axes=(0, 3, 2, 1))
        # elif self.nr_phys == 2 and not self.on_aux:
        #     Fr = self.F[(nr, n2)].fuse_legs(axes=(0, 1, (2, 3)))
        #     tmp = tmp.fuse_legs(axes=(0, 1, (2, 5), 3, 4))
        #     tmp = tmp.fuse_legs(axes=((0, 2), 1, 3, 4))
        #     tmp = tmp.fuse_legs(axes=((0, 1), 2, 3))
        #     tmp = tmp.tensordot(Fr, axes=(2, 0))
        #     tmp = self.op[n2]._attach_23(tmp)
        #     tmp = tmp.fuse_legs(axes=(0, 1, (2, 3)))
        #     tmp = tmp.unfuse_legs(axes=0)
        #     tmp = self.op[n1]._attach_23(tmp)
        #     tmp = tmp.unfuse_legs(axes=2)
        #     tmp = tmp.unfuse_legs(axes=(0, 2))
        #     tmp = tmp.swap_gate(axes=(1, 3))
        #     tmp = self.F[(nl, n1)].tensordot(tmp, axes=((3, 1, 2), (0, 2, 3)))
        #     tmp = tmp.unfuse_legs(axes=1)
        #     tmp = tmp.transpose(axes=(0, 5, 1, 4, 3, 2))
        # else:  # if self.nr_phys == 2 and self.on_aux:  todo
        #     tmp = tmp.fuse_legs(axes=(0, 2, (1, 3, 4), 5))
        #     tmp = tmp.fuse_legs(axes=(0, 1, (2, 3)))
        #     tmp = ncon([tmp, self.F[(nl, n1)]], ((1, -1, -0), (-3, -2, 1)))
        #     tmp = self.op[n1]._attach_01(tmp)
        #     tmp = tmp.fuse_legs(axes=(0, 1, (2, 3)))
        #     tmp = tmp.unfuse_legs(axes=0)
        #     tmp = self.op[n2]._attach_01(tmp)
        #     tmp = tmp.unfuse_legs(axes=0)
        #     tmp = ncon([tmp, self.F[(nr, n2)]], ((-1, -2, 1, 2, -0, -4), (1, 2, -3)))
        #     tmp = tmp.unfuse_legs(axes=0).transpose(axes=(0, 2, 1, 3, 4, 5))
        return self.op.factor * self._project_ort(tmp)

    def hole(self, n):
        """ Hole for peps tensor at site n. """
        nl, nr = n - 1, n + 1
        if self.nr_phys == 1:
            tmp = self.F[(nl, n)].tensordot(self.ket[n], axes=(3, 0))
            tmp = tmp.tensordot(self.F[(nr, n)], axes=((2, 4), (2, 0)))
            tmp = tmp.tensordot(self.bra[n].conj(), axes=((0, 4), (0, 2)))
            return tmp.transpose(axes=(0, 3, 2, 1))

    def clear_site_(self, *args):
        return _clear_site_(self.F, *args)

    def measure(self, bd=None):
        if bd is None:
            bd = (-1, 0)
        axes = ((0, 1, 2, 3), (3, 1, 2, 0))
        return self.factor() * self.F[bd].tensordot(self.F[bd[::-1]], axes=axes).to_number()

    def update_env_(self, n, to='last'):
        if self.nr_phys == 1 and to == 'last':
            bran = self.bra[n].transpose(axes=(2, 1, 0)).conj()
            tmp = self.F[(n - 1, n)].fuse_legs(axes=(0, 1, (2, 3)))
            tmp = bran.tensordot(tmp, axes=(2, 0))
            tmp = self.op[n]._attach_01(tmp)
            tmp = tmp.unfuse_legs(axes=2)
            self.F[(n, n + 1)] = tmp.tensordot(self.ket[n], axes=((3, 4), (0, 1)))
        elif self.nr_phys == 1 and to == 'first':
            tmp = self.F[(n + 1, n)].fuse_legs(axes=(0, 1, (2, 3)))
            tmp = self.ket[n].tensordot(tmp, axes=(2, 0))
            tmp = self.op[n]._attach_23(tmp)
            tmp = tmp.unfuse_legs(axes=2)
            self.F[(n, n - 1)] = tmp.tensordot(self.bra[n].conj(), axes=((3, 4), (2, 1)))
        # elif nr_phys == 2 and not on_aux and to == 'last':
        #     bran = bra[n].fuse_legs(axes=((2, 3), 1, 0)).conj()
        #     tmp = F[(n - 1, n)].fuse_legs(axes=(0, 1, (2, 3)))
        #     tmp = bran.tensordot(tmp, axes=(2, 0))
        #     tmp = op[n]._attach_01(tmp)
        #     tmp = tmp.unfuse_legs(axes=(0, 2))
        #     tmp = tmp.swap_gate(axes=(1, 3))
        #     F[(n, n + 1)] = tmp.tensordot(ket[n], axes=((4, 5, 1), (0, 1, 3)))
        # elif nr_phys == 2 and not on_aux and to == 'first':
        #     ketn = ket[n].fuse_legs(axes=((0, 3), 1, 2))
        #     tmp = F[(n + 1, n)].fuse_legs(axes=(0, 1, (2, 3)))
        #     tmp = ketn.tensordot(tmp, axes=(2, 0))
        #     tmp = op[n]._attach_23(tmp)
        #     tmp = tmp.unfuse_legs(axes=(0, 2))
        #     tmp = tmp.swap_gate(axes=(1, 3))
        #     F[(n, n - 1)] = tmp.tensordot(bra[n].conj(), axes=((1, 4, 5), (3, 2, 1)))
        # elif nr_phys == 2 and on_aux and to == 'last':  # todo
        #     tmp = ncon([ket[n], F[(n - 1, n)]], ((1, -4, -0, -1), (-3, -2, 1)))
        #     tmp = tmp.fuse_legs(axes=(0, 1, 2, (3, 4)))
        #     tmp = op[n]._attach_01(tmp)
        #     bA = bra[n].fuse_legs(axes=((0, 1), 2, 3))
        #     F[(n, n + 1)] = ncon([bA.conj(), tmp], ((1, -0, 2), (-2, -1, 1, 2)))
        # else: # nr_phys == 2 and on_aux and to == 'first':  # todo
        #     bA = bra[n].fuse_legs(axes=((0, 1), 2, 3))
        #     tmp = ncon([bA.conj(), F[(n + 1, n)]], ((-0, 1, -1), (-3, -2, 1)))
        #     tmp = op[n]._attach_23(tmp)
        #     tmp = tmp.unfuse_legs(axes=0)
        #     F[(n, n - 1)] = ncon([ket[n], tmp], ((-0, 1, 2, 3), (-2, 1, -1, 2, 3)))

        if self.projector:
            self.projector._update_env(n,to)

