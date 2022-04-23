# class that represent a model (lut or gmf)

from abc import abstractmethod
import numpy as np
import xarray as xr


class Model:
    """
    Model class is an abstact class, for handling GMF or LUT.
    It should not be instanciated by the user.

    All registered models can be retreived with :func:`~xsarsea.windspeed.available_models`
    """

    _available_models = {}

    @abstractmethod
    def __init__(self, name, **kwargs):
        self.name = name
        self.pol = kwargs.pop('pol', None)
        self.units = kwargs.pop('units', None)
        self.inc_range = kwargs.pop('inc_range', None)
        self.phi_range = kwargs.pop('phi_range', None)
        self.wspd_range = kwargs.pop('wspd_range', None)
        self.__class__._available_models[name] = self

    @abstractmethod
    def _raw_lut(self):
        pass

    @staticmethod
    def _check_lut(lut):
        # check that lut is correct
        assert isinstance(lut, xr.DataArray)
        try:
            units = lut.attrs['units']
        except KeyError:
            raise KeyError("lut has no lut.attrs['units']")

        allowed_units = ['linear', 'dB']
        if units not in allowed_units:
            raise ValueError("Unknown lut units '%s'. Allowed are '%s'" % (units, allowed_units))

        if lut.ndim == 2:
            good_dims = ('incidence', 'wspd')
            if not (lut.dims == good_dims):
                raise IndexError("Bad dims '%s'. Should be '%s'" % (lut.dims, good_dims))
        elif lut.ndim == 3:
            good_dims = ('incidence', 'wspd', 'phi')
            if not (lut.dims == good_dims):
                raise IndexError("Bad dims '%s'. Should be '%s'" % (lut.dims, good_dims))
        else:
            raise IndexError("Bad dims '%s'" % lut.dims)

        return True

    @property
    def iscopol(self):
        """True if model is copol"""
        return len(set(self.pol)) == 1

    @property
    def iscrosspol(self):
        """True if model is crosspol"""
        return len(set(self.pol)) == 2

    def to_lut(self, units='linear'):
        """
        Get the model lut

        Parameters
        ----------
        units: str
            'linear', 'dB'

        Returns
        -------
        xarray.DataArray

        """

        lut = self._raw_lut()
        self._check_lut(lut)
        final_lut = lut

        if units is None:
            return final_lut

        if units == 'dB':
            if lut.attrs['units'] == 'dB':
                final_lut = lut
            elif lut.attrs['units'] == 'linear':
                final_lut = 10 * np.log10(lut + 1e-15)  # clip with epsilon to avoid nans
                final_lut.attrs['units'] = 'dB'
        elif units == 'linear':
            if lut.attrs['units'] == 'linear':
                final_lut = lut
            elif lut.attrs['units'] == 'dB':
                final_lut = 10. ** (lut / 10.)
                final_lut.attrs['units'] = 'linear'
        else:
            raise ValueError("Unit not known: %s. Known are 'dB' or 'linear' " % units)

        final_lut.attrs['model'] = self.name

        return final_lut

    @abstractmethod
    def __call__(self, inc, wspd, phi=None, broadcast=False):
        """

        Parameters
        ----------
        inc: array-like
            incidence
        wspd: array-like
            windspeed
        phi: array-like or None
            phi
        broadcast: bool
            | If True, input arrays will be broadcasted to the same dimensions, and output will have the same dimension.
            | This option is only available for :func:`~xsarsea.windspeed.gmfs.GmfModel`

        Examples
        --------

        >>> cmod5 = xsarsea.windspeed.get_model('cmod5')
        >>> cmod5(np.arange(20,22), np.arange(10,12))
        <xarray.DataArray (incidence: 2, wspd: 2)>
        array([[0.00179606, 0.00207004],
        [0.0017344 , 0.00200004]])
        Coordinates:
        * incidence  (incidence) int64 20 21
        * wspd       (wspd) int64 10 11
        Attributes:
        units:    linear

        Returns
        -------
        xarray.DataArray
        """
        raise NotImplementedError(self.__class__)

    def __repr__(self):
        return "<%s('%s') pol=%s>" % (self.__class__.__name__, self.name, self.pol)


class LutModel(Model):
    """
    Abstract class for handling Lut models. See :func:`~Model`

    Examples
    --------

    >>> isinstance(xsarsea.windspeed.get_model('sarwing_lut_cmodms1ahw'), LutModel)
    True
    """

    def __call__(self, inc, wspd, phi=None, units=None):

        all_scalar = all(np.isscalar(v) for v in [inc, wspd, phi] if v is not None)

        all_1d = False
        try:
            all_1d = all(v.ndim == 1 for v in [inc, wspd, phi] if v is not None)
        except AttributeError:
            all_1d = False

        if not(all_scalar or  all_1d):
            raise NotImplementedError('Only scalar or 1D array are implemented for LutModel')

        lut = self.to_lut(units=units)
        if 'phi' in lut.dims:
            sigma0 = lut.interp(incidence=inc, wspd=wspd, phi=phi)
        else:
            sigma0 = lut.interp(incidence=inc, wspd=wspd)

        try:
            sigma0.name = 'sigma0_gmf'
            sigma0.attrs['model'] = self.name
            sigma0.attrs['units'] = self.units
        except AttributeError:
            pass

        if all_scalar:
            return sigma0.item()
        else:
            return sigma0


def available_models(pol=None):
    """
    get available models

    Parameters
    ----------
    pol: str or None
        Filter models by pol (ie pol='VV')
        if None, no filters

    Returns
    -------
    dict
        dict of available models. Key is name, value is model.

    """
    if pol is None:
        return Model._available_models
    else:
        models_found = {}
        for name, model in Model._available_models.items():
            if pol == model.pol:
                models_found[name] = model
        return models_found

def get_model(name):
    """
    get model by name

    Parameters
    ----------
    name: str
        model name

    Returns
    -------
    Model
    """

    if isinstance(name, Model):
        # name is already a model
        return name

    return available_models()[name]
