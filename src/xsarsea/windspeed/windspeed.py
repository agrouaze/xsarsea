import sys
from numba import  vectorize, float64, complex128
import numpy as np
import warnings
import xarray as xr
from ..xsarsea import logger, timing
from .gmfs import _gmf_to_lut
from .sarwing_luts import lut_from_sarwing


def wind_to_img(u, v, ground_heading, convention='antenna'):
    wind_azi = np.sqrt(u ** 2 + v ** 2) * \
               np.exp(1j * (np.arctan2(u, v) - np.deg2rad(ground_heading)))

    wind_azi.attrs['comment'] = """
        Ancillary wind, as a complex number.
        complex angle is the wind direction relative to azimuth (atrack)
        module is windspeed
        real part is atrack wind component
        imag part is xtrack wind component
        """

    if convention == 'antenna':
        # transpose real and img to get antenna convention
        wind_antenna = np.imag(wind_azi) + 1j * np.real(wind_azi)
        wind_antenna.attrs['comment'] = """
                Ancillary wind, as a complex number.
                complex angle is the wind direction relative to antenna (xtrack)
                module is windspeed
                real part is antenna (xtrack) wind component 
                imag part is atrack wind component
                """
        return wind_antenna
    else:
        return wind_azi


def nesz_flattening(noise, inc):
    """

    Parameters
    ----------
    noise: array-like
        noise array (nesz), with shape (atrack, xtrack)
    inc: array-like
        incidence array

    Returns
    -------
    array-like
        flattened noise

    """

    if noise.ndim != 2:
        raise IndexError('Only 2D noise allowed')

    with warnings.catch_warnings():
        warnings.filterwarnings('ignore', message='.*empty.*', category=RuntimeWarning)
        noise_mean = np.nanmean(noise, axis=0)

    try:
        # unlike np.mean, np.nanmean return a numpy array, even if noise is an xarray
        # if this behaviour change in the future, we want to be sure to have a numpy array
        noise_mean = noise_mean.values
    except AttributeError:
        pass

    def _noise_flattening_1row(noise_row, inc_row):

        noise_flat = noise_row.copy()

        # replacing nan values by nesz mean value for concerned incidence
        noise_flat[np.isnan(noise_flat)] = noise_mean[np.isnan(noise_flat)]

        # to dB
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            noise_db = 10. * np.log10(noise_flat)

        try:
            _coef = np.polyfit(inc_row[np.isfinite(noise_db)],
                               noise_db[np.isfinite(noise_db)], 1)
        except TypeError:
            # noise is all nan
            return np.full(noise_row.shape, np.nan)

        # flattened, to linear
        noise_flat = 10. ** ((inc_row * _coef[0] + _coef[1] - 1.0) / 10.)

        return noise_flat

    # incidence is almost constant along atrack dim, so we can make it 1D
    return np.apply_along_axis(_noise_flattening_1row, 1, noise, np.nanmean(inc, axis=0))



def get_lut(name, inc_range=None, phi_range=None, wspd_range=None, allow_interp=True, db=True):
    sarwing_error = None
    lut = None

    # try to load lut from sarwing

    if name.startswith('sarwing_lut_'):
        lut = lut_from_sarwing(name)
        if not db:
            lut = 10. ** (lut / 10.)  # to linear
            lut.attrs['units'] = 'linear'

    if name.startswith('gmf_'):
        lut = _gmf_to_lut(name, inc_range=inc_range, phi_range=phi_range, wspd_range=wspd_range, allow_interp=allow_interp)
        if db:
            lut = 10 * np.log10(np.clip(lut, 1e-15, None))  # clip with epsilon to avoid nans
            lut.attrs['units'] = 'dB'

    if lut is None:
        raise ValueError('%s not found in gmfs or sarwing luts' % name)

    return lut




@timing
def invert_from_model(*args, **kwargs):
    # default array values if no crosspol
    nan = args[1] * np.nan
    #default_gmf = ('cmod5n', 'cmodms1ahw')

    if len(args) == 3:
        # copol inversion
        inc, sigma0_co, ancillary_wind = args
        sigma0_cr = nan
        nesz_cr = nan

    elif len(args) == 5:
        # dualpol inversion
        inc, sigma0_co, sigma0_cr, nesz_cr, ancillary_wind = args
    else:
        raise TypeError("invert_from_model() takes 3 or 5 positional arguments, but %d were given" % len(args))

    models_names = kwargs.pop('model', None)
    if not isinstance(models_names, tuple):
        models_names = (models_names, None)

    # to dB
    sigma0_co_db = 10 * np.log10(sigma0_co + 1e-15)
    if sigma0_cr is not nan:
        sigma0_cr_db = 10 * np.log10(sigma0_cr + 1e-15)
    else:
        sigma0_cr_db = nan
    if nesz_cr is not nan:
        nesz_cr_db = 10 * np.log10(nesz_cr + 1e-15)
    else:
        nesz_cr_db = nan

    def _invert_from_model_numpy(np_inc, np_sigma0_co_db, np_sigma0_cr_db, np_nesz_cr_db, np_ancillary_wind):
        # this wrapper function is only useful if using dask.array.map_blocks:
        # local variables defined here will be available on the worker, and they will be used
        # in _invert_copol_numba


        dsig_co = 0.1
        d_antenna = 2
        d_azi = 2
        dwspd_fg = 2


        sigma0_co_lut_db = get_lut(models_names[0])  # shape (inc, wspd, phi)
        np_sigma0_co_lut_db = np.ascontiguousarray(np.asarray(sigma0_co_lut_db.transpose('wspd', 'phi', 'incidence')))
        np_wspd_dim = np.asarray(sigma0_co_lut_db.wspd)
        np_phi_dim = np.asarray(sigma0_co_lut_db.phi)
        np_inc_dim = np.asarray(sigma0_co_lut_db.incidence)

        np_phi_lut, np_wspd_lut = np.meshgrid(np_phi_dim, np_wspd_dim)  # shape (wspd,phi)
        np_wspd_lut_co_antenna = np_wspd_lut * np.cos(np.radians(np_phi_lut))  # antenna (xtrack)
        np_wspd_lut_co_azi = np_wspd_lut * np.sin(np.radians(np_phi_lut))  # azi (atrack)

        if not np.all(np.isnan(np_sigma0_cr_db)):
            sigma0_cr_lut_db = get_lut(models_names[1])
            np_sigma0_cr_lut_db = np.ascontiguousarray(np.asarray(sigma0_cr_lut_db.transpose('wspd', 'incidence')))
            np_wspd_lut_cr = np.asarray(sigma0_cr_lut_db.wspd)
            np_inc_cr_dim = np.asarray(sigma0_cr_lut_db.incidence)
        else:
            # dummy empty for numba typing
            np_inc_cr_dim = np.array([], dtype=np.float64)
            np_wspd_lut_cr = np.array([], dtype=np.float64)
            np_sigma0_cr_lut_db = np.array([[]], dtype=np.float64)



        if (180 - (np_phi_dim[-1] - np_phi_dim[0])) <2:
            # phi is in range [ 0, 180 ] (symetrical lut)
            phi_180 = True
        else:
            phi_180 = False



        def __invert_from_model_scalar(one_inc, one_sigma0_co_db, one_sigma0_cr_db, one_nesz_cr_db, one_ancillary_wind):
            # invert from gmf for scalar (float) input.
            # this function will be vectorized with 'numba.vectorize' or 'numpy.frompyfunc'
            # set debug=True below to force 'numpy.frompyfunc', so you can debug this code

            if np.isnan(one_sigma0_co_db) or np.isnan(one_inc):
                return np.nan

            i_inc = np.argmin(np.abs(np_inc_dim-one_inc))
            np_sigma0_co_lut_db_inc = np_sigma0_co_lut_db[:, :, i_inc]

            # get wind dir components, relative to antenna and azi
            m_antenna = np.real(one_ancillary_wind)  # antenna (xtrack)
            m_azi = np.imag(one_ancillary_wind)  # azi (atrack)
            if phi_180:
                m_azi = np.abs(m_azi)  # symetrical lut
            Jwind_co = ((np_wspd_lut_co_antenna - m_antenna) / d_antenna) ** 2 + \
                       ((np_wspd_lut_co_azi - m_azi) / d_azi) ** 2  # shape (phi, wspd)
            Jsig_co = ((np_sigma0_co_lut_db_inc - one_sigma0_co_db) / dsig_co) ** 2  # shape (wspd, phi)
            J_co = Jwind_co + Jsig_co
            ## cost function
            iJ_co = np.argmin(J_co)
            lut_idx = (iJ_co // J_co.shape[-1], iJ_co % J_co.shape[-1])
            wspd = np_wspd_lut[lut_idx]

            if not np.isnan(one_sigma0_cr_db):
                # crosspol available, do dualpol inversion
                i_inc = np.argmin(np.abs(np_inc_cr_dim - one_inc))
                np_sigma0_cr_lut_db_inc = np_sigma0_cr_lut_db[:, i_inc]

                Jwind_cr = ((np_wspd_lut_cr - wspd) / dwspd_fg) ** 2.
                nrcslin = 10. ** (one_sigma0_cr_db / 10.)
                neszlin = 10. ** (one_nesz_cr_db / 10.)
                dsig_cr = (1.25 / (nrcslin / neszlin)) ** 4.
                Jsig_cr = ((np_sigma0_cr_lut_db_inc - one_sigma0_cr_db) / dsig_cr) ** 2.
                J_cr = Jsig_cr + Jwind_cr
                # numba doesn't support nanargmin
                # having nan in J_cr is an edge case, but if some nan where provided to analytical
                # function, we have to handle it
                # J_cr[np.isnan(J_cr)] = np.nanmax(J_cr)
                spd_dual = np_wspd_lut_cr[np.argmin(J_cr)]
                if (spd_dual > 5) and (wspd > 5):
                    wspd = spd_dual

            return wspd

        # build a vectorized function from __invert_from_gmf_scalar
        debug = sys.gettrace()
        # debug = True  # force np.frompyfunc
        # debug = False
        if debug:
            __invert_from_model_vect = timing(np.frompyfunc(__invert_from_model_scalar, 5, 1))
        else:
            # fastmath can be used, but we will need nan handling
            __invert_from_model_vect = timing(
                vectorize([float64(float64, float64, float64, float64, complex128)], fastmath={'nnan': False}, target='parallel')
                (__invert_from_model_scalar)
            )
        with warnings.catch_warnings():
            warnings.filterwarnings('ignore', message='.*invalid value encountered.*', category=RuntimeWarning)
            return __invert_from_model_vect(np_inc, np_sigma0_co_db,
                                            np_sigma0_cr_db, np_nesz_cr_db, np_ancillary_wind)


    def _invert_from_model_any(inc, sigma0_co_db, sigma0_cr_db, nesz_cr_db, ancillary_wind):
        # wrapper to allow computation on any type (xarray, numpy)

        try:
            # if input is xarray, will return xarray
            da_ws = xr.zeros_like(sigma0_co_db)
            da_ws.name = 'windspeed_gmf'
            da_ws.attrs.clear()
            try:
                # if dask array, use map_blocks
                # raise ImportError
                import dask.array as da
                if all(
                        [
                            isinstance(v.data, da.Array)
                            for v in [inc, sigma0_co_db, sigma0_cr_db, nesz_cr_db, ancillary_wind]
                        ]
                ):
                    da_ws.data = da.map_blocks(
                        _invert_from_model_numpy,
                        inc.data, sigma0_co_db.data, sigma0_cr_db.data, nesz_cr_db.data, ancillary_wind.data,
                        meta=sigma0_co_db.data
                    )
                    logger.debug('invert with map_blocks')
                else:
                    raise TypeError

            except (ImportError, TypeError):
                # use numpy array, but store in xarray
                da_ws.data = _invert_from_model_numpy(
                    np.asarray(inc),
                    np.asarray(sigma0_co_db),
                    np.asarray(sigma0_cr_db),
                    np.asarray(nesz_cr_db),
                    np.asarray(ancillary_wind),
                )
                logger.debug('invert with xarray.values. no chunks')
        except TypeError:
            # full numpy
            logger.debug('invert with numpy')
            da_ws = _invert_from_model_numpy(
                inc,
                sigma0_co_db,
                sigma0_cr_db,
                nesz_cr_db,
                ancillary_wind
            )

        return da_ws

    # main
    ws = _invert_from_model_any(inc, sigma0_co_db, sigma0_cr_db, nesz_cr_db, ancillary_wind)
    return ws