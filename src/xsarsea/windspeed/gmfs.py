import numpy as np
import warnings
from ..xsarsea import logger, timing
from functools import lru_cache
from numba import njit, vectorize, guvectorize, float64, float32
import xarray as xr
from .sarwing_luts import load_sarwing_lut
from .utils import cmod_descr




@timing
@lru_cache
def _get_gmf_func(name, ftype='numba_vectorize'):
    """
     get function for gmf `name`

    Parameters
    ----------
    name: str
        gmf name

    ftype: str
        return function type. Allowed values are:

            - 'numba_vectorize': vectorized with `numba.vectorize` (default)
            - 'numba_guvectorize': vectorized with `numba.guvectorize`
            - 'vectorize': vectorized with `numpy.vectorize`, without signature
            - 'guvectorize': vectorized with `numpy.vectorize`, with signature
            - 'numba_njit': compiled with `numba.njit` (only scalar input will be allowed)
            - None: pure python function (only scalar input will be allowed)


    Returns
    -------
    function
        `sigma0_linear = function(inc, u10, [phi])`
    """

    try:
        gmf_attrs = cmod_descr[name].copy()
    except KeyError:
        raise KeyError('gmf %s not available. (available: %s)' % (name, cmod_descr.keys()))

    pyfunc = gmf_attrs['gmf']

    if pyfunc is None:
        raise NotImplementedError('no gmf implementation for %s' % name)

    if ftype is None:
        return pyfunc

    if ftype == 'numba_njit':
        return njit([float64(float64, float64, float64)], nogil=True, inline='never')(pyfunc)

    if ftype == 'numba_vectorize':
        return vectorize(
            [
                float64(float64, float64, float64),
                float32(float32, float32, float64)
            ], target='parallel', nopython=True)(pyfunc)

    if ftype == 'numba_guvectorize':
        func_njit = _get_gmf_func(name, ftype='numba_njit')

        @guvectorize(
            [
                (float64[:], float64[:], float64[:], float64[:, :, :]),
                (float32[:], float32[:], float32[:], float32[:, :, :])
            ], '(n),(m),(p)->(n,m,p)', target='cpu')
        def func(inc, u10, phi, sigma0_out):
            for i_phi, one_phi in enumerate(phi):
                for i_u10, one_u10 in enumerate(u10):
                    for i_inc, one_inc in enumerate(inc):
                        sigma0_out[i_inc, i_u10, i_phi] = func_njit(one_inc, one_u10, one_phi)

        return func

    raise TypeError('ftype "%s" not known')


@timing
def gmf(inc, u10, phi=None, name=None, numba=True):
    try:
        assert cmod_descr[name]['gmf'] is not None
    except (KeyError, AssertionError):
        available_cmod = [k for k in cmod_descr.keys() if cmod_descr[k]['gmf'] is not None]
        raise ValueError("gmf name must be one of %s, and have an analytical function" % available_cmod)


    # input ndim give the function ftype
    try:
        ndim = u10.ndim
    except AttributeError:
        # scalar input
        ndim = 0

    if numba:
        if ndim == 0:
            ftype = 'numba_njit'
        elif ndim == 1:
            ftype = 'numba_guvectorize'
        else:
            ftype = 'numba_vectorize'
    else:
        if ndim == 0:
            ftype = None
        elif ndim == 1:
            ftype = 'guvectorize'
        else:
            ftype = 'vectorize'

    gmf_func = _get_gmf_func(name, ftype=ftype)

    # every gmf needs a phi, even for crosspol, but we will squeeze it after compute (for guvectorize function)
    squeeze_phi_dim = (phi is None) and (ndim == 1)
    if squeeze_phi_dim:
        phi = np.array([np.nan])
    if phi is None:
        # non guvectorized function with no phi
        phi = u10 * np.nan

    sigma0_lin = gmf_func(inc, u10, phi)
    if squeeze_phi_dim:
        sigma0_lin = np.squeeze(sigma0_lin, axis=2)

    # add name and comment to variable, if xarray
    try:
        sigma0_lin.name = 'sigma0_gmf'
        sigma0_lin.attrs['comment'] = "sigma0_gmf from '%s'" % name
        sigma0_lin.attrs['units'] = 'linear'
    except AttributeError:
        pass

    return sigma0_lin


@timing
def _gmf_lut(name, inc_range=None, phi_range=None, u10_range=None, allow_interp=True):

    try:
        cmod_attrs = cmod_descr[name].copy()
    except KeyError:
        raise KeyError('gmf %s not available. (available: %s)' % (name, cmod_descr.keys()))

    inc_range = inc_range or cmod_attrs.pop('inc_range')
    phi_range = phi_range or cmod_attrs.pop('phi_range', None)
    u10_range = u10_range or cmod_attrs.pop('u10_range')

    inc_step_hr = 0.1
    u10_step_hr = 0.1
    phi_step_hr = 1

    inc_step_lr = 0.2
    u10_step_lr = 0.5
    phi_step_lr = 1

    if allow_interp:
        inc_step = inc_step_lr
        u10_step = u10_step_lr
        phi_step = phi_step_lr
    else:
        inc_step = inc_step_hr
        u10_step = u10_step_hr
        phi_step = phi_step_hr

    # 2*step, because we want to be sure to not have bounds conditions in interp
    inc = np.arange(inc_range[0] - inc_step, inc_range[1] + 2*inc_step, inc_step)
    u10 = np.arange(np.max([0, u10_range[0] - u10_step]), u10_range[1] + 2*u10_step, u10_step)

    try:
        phi = np.arange(phi_range[0], phi_range[1] + 2*phi_step, phi_step)
        dims = ['incidence', 'u10', 'phi']
        coords = {'incidence': inc, 'u10': u10, 'phi': phi}
    except TypeError:
        phi = None
        dims = ['incidence', 'u10']
        coords = {'incidence': inc, 'u10': u10}


    lut = xr.DataArray(
        gmf(inc, u10, phi, name),
        dims=dims,
        coords=coords
    )

    if allow_interp:
        # interp to get high res
        interp_kwargs = {}
        interp_kwargs['incidence'] = np.arange(inc_range[0], inc_range[1] + inc_step_hr, inc_step_hr)
        interp_kwargs['u10'] = np.arange(u10_range[0], u10_range[1] + u10_step_hr, u10_step_hr)
        if phi is not None:
            interp_kwargs['phi'] = np.arange(phi_range[0], phi_range[1] + phi_step_hr, phi_step_hr)

        lut = lut.interp(**interp_kwargs, kwargs=dict(bounds_error=True))

    # crop lut to exact range
    crop_cond = (lut.incidence >= inc_range[0]) & (lut.incidence <= inc_range[1])
    crop_cond = crop_cond & (lut.u10 >= u10_range[0]) & (lut.u10 <= u10_range[1])
    if phi is not None:
        crop_cond = crop_cond & (lut.phi >= phi_range[0]) & (lut.phi <= phi_range[1])

    lut = lut.where(crop_cond, drop=True)

    if np.count_nonzero(np.isnan(lut.values)):
        raise ValueError('Found nans in lut %s' % name)


    # TODO add gmf_lut.attrs

    return lut


def gmf_lut(name, inc_range=None, phi_range=None, u10_range=None, allow_interp=True, sarwing=None, db=True):
    sarwing_error = None
    lut = None

    if sarwing:
        # try to load lut
        try:
            lut = load_sarwing_lut(cmod_descr[name]['lut_path'])
            if not db:
                lut = 10. ** (lut / 10.)  # to linear
                lut.attrs['units'] = 'linear'
        except (FileNotFoundError, KeyError) as e:
            sarwing_error = e

    if lut is None:
        lut = _gmf_lut(name, inc_range=inc_range, phi_range=phi_range, u10_range=u10_range, allow_interp=allow_interp)
        if db:
            lut = 10 * np.log10(np.clip(lut, 1e-15, None))  # clip with epsilon to avoid nans
            lut.attrs['units'] = 'dB'
    elif sarwing_error is not None:
        raise sarwing_error

    if np.count_nonzero(np.isnan(lut)):
        raise ValueError('Found nans in lut')

    return lut



