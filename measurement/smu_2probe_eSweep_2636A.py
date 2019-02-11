from .measurement import register, AbstractMeasurement, Contacts, PlotRecommendation
from .measurement import StringValue, FloatValue, IntegerValue, DatetimeValue, AbstractValue, SignalInterface, GPIBPathValue

import numpy as np
from datetime import datetime
from threading import Event
import time
from typing import Dict, Tuple, List
from typing.io import TextIO

from .smu_2probe_2636A import SMU2Probe2636A
from scientificdevices.oxford.itc503 import ITC
import gpib


class GenericInstrument(object):
    """ This is an abstract class for a generic instrument """
    def __init__(self):
        """ Initialises the generic instrument """
        self.term_chars = '\n'

    def ask(self, query):
        """ ask will write a request and waits for an answer

            Arguments:
            query -- (string) the query which shall be sent

            Result:
            (string) -- answer from device
        """
        self.write(query)
        return self.read()

    def write(self, query):
        """ writes a query to remote device

            Arguments:
            query -- (string) the query which shall be sent
        """
        pass

    def read(self):
        """ reads a message from remote device

            Result:
            (string) -- message from remote device
        """
        pass

    def close(self):
        """ closes connection to remote device """
        pass

class GpibInstrument(GenericInstrument):
    """ Implementation of GenericInstrument to communicate with gpib devices """
    def __init__(self, device):
        """ initializes connection to gpib device

            Arguments:
            connection - (gpib.dev) a gpib object to speak to
        """
        GenericInstrument.__init__(self)
        self.device = device
        self.term_chars = '\n'

    def write(self, query):
        """ writes a query to remote device

            Arguments:
            query -- (string) the query which shall be sent
        """
        gpib.write(self.device, query + self.term_chars)

    def read(self):
        """ reads a message from remote device

            Result:
            (string) -- message from remote device
        """
        return gpib.read(self.device, 512).rstrip()

    def close(self):
        """ closes connection to remote device """
        gpib.close(self.device)

    def clear(self):
        """ clears all communication buffers """
        gpib.clear(self.device)

def get_gpib_timeout(timeout):
    """ returns the correct timeout object to a certain timeoutvalue
        it will find the nearest match, e.g., 120us will be 100us

        Arguments:
        timeout -- (float) number of seconds to wait until timeout
    """
    gpib_timeout_list = [(0, gpib.TNONE), \
                         (10e-6, gpib.T10us), \
                         (30e-6, gpib.T30us), \
                         (100e-6, gpib.T100us), \
                         (300e-6, gpib.T300us), \
                         (1e-3, gpib.T1ms), \
                         (3e-3, gpib.T3ms), \
                         (10e-3, gpib.T10ms), \
                         (30e-3, gpib.T30ms), \
                         (100e-3, gpib.T100ms), \
                         (300e-3, gpib.T300ms), \
                         (1, gpib.T1s), \
                         (3, gpib.T3s), \
                         (10, gpib.T10s), \
                         (30, gpib.T30s), \
                         (100, gpib.T100s), \
                         (300, gpib.T300s), \
                         (1000, gpib.T1000s)]

    for val, res in gpib_timeout_list:
        if timeout <= val:
            return res
    return gpib.T1000s

def get_gpib_device(port: int, timeout=0.5):
    device = gpib.dev(0, port)
    gpib.timeout(device, get_gpib_timeout(timeout))
    return GpibInstrument(device)



@register('SourceMeter two probe voltage hysteresis loop with 2636A')
class SMU2ProbeESweep2636A(SMU2Probe2636A):
    """
    Voltage driven 2-probe current measurement on a sourcemeter.
    Performing a hysteresis loop for ferroelectric samples.
    """


    def __init__(self, *args, **kwargs) -> None:
        self._loop_count = kwargs.pop("loop_count")
        self._sample_name = kwargs.pop("sample_name")
        super().__init__(*args, **kwargs)
        
        # Initialise the ITC503 Temperature Controller
        self._temp = ITC(get_gpib_device(24))

    @staticmethod
    def inputs() -> Dict[str, AbstractValue]:
        '''
        SMU 2636A needs additional setting for minimal measurement range.
        '''  
        inputs = SMU2Probe2636A.inputs()
        inputs['loop_count'] = IntegerValue('Number of hysteresis loops', default=1)
        inputs['sample_name'] = StringValue('Sample Name', default='ChipXX_depNoXX')
        return inputs


    @property
    def recommended_plots(self) -> List[PlotRecommendation]:
        return [PlotRecommendation('E-Field-Sweep', x_label='v', y_label='i', show_fit=True)]
        

    def _generate_file_name_prefix(self) -> str:
        return '{}_eSweepHysteresis_contacts_{}_'.format(self._sample_name, '--'.join(self._contacts))

    def _generate_plot_file_name_prefix(self, pair) -> str:
        return '{}_eSweepHysteresis_contacts_{}_plot-{}-{}_'.format(self._sample_name, '--'.join(self._contacts), pair[0], pair[1])
        
    def _measure(self, file_handle) -> None:
        """
        Custom measurement code lives here.
        """
        self.__write_header(file_handle)
        self.__initialize_device()
        time.sleep(0.5)

        voltage_space = self._setup_voltage_space(loop_count=self._loop_count)
        
        self._sweep_voltage(voltage_space, file_handle)

        self.__deinitialize_device()

    
    def _setup_voltage_space(self, loop_count=1):
        '''
        Voltage values:
        0 -> max-voltage,
        max_voltage -> -max_voltage
        -max_voltage -> 0
        '''
        zero_to_max = np.linspace(0, self._max_voltage, self._number_of_points)
        max_to_zero = np.linspace(self._max_voltage, 0, self._number_of_points)
        zero_to_min = np.linspace(0, -1*self._max_voltage, self._number_of_points)
        min_to_zero = np.linspace(-1*self._max_voltage, 0, self._number_of_points)
        
        # np.tile() aggregates a np.array with values repeated loop_count number of times.
        middle_loop = np.tile(np.concatenate((max_to_zero, zero_to_min, min_to_zero, zero_to_max)), loop_count)

        return np.concatenate((zero_to_max, middle_loop, max_to_zero))
        

    def _sweep_voltage(self, voltage_space, file_handle):
        
        for voltage in voltage_space:
            if self._should_stop.is_set():
                print("DEBUG: Aborting measurement.")
                self._signal_interface.emit_aborted()
                break

            self._device.set_voltage(voltage)
            time.sleep(15)
            self._acquire_data_point(file_handle)



    def _acquire_data_point(self, file_handle):
        meas_voltage, meas_current = self.__measure_data_point()
        T3 = self._temp.T3
        
        file_handle.write('{} {} {} {} \n'.format(datetime.now().isoformat(), 
                                                       meas_voltage, meas_current, T3))
        file_handle.flush()
        
        self._signal_interface.emit_data({'v': meas_voltage, 'i': meas_current, 'datetime': datetime.now()})


    def __write_header(self, file_handle: TextIO) -> None:
        """Write a file header for present settings.

        Arguments:
            file_handle: The open file to write to
        """
        file_handle.write("# {0}\n".format(datetime.now().isoformat()))
        file_handle.write('# {}\n'.format(self._comment))
        file_handle.write("# maximum voltage {0} V\n".format(self._max_voltage))
        file_handle.write("# current limit {0} A\n".format(self._current_limit))
        file_handle.write('# nplc {}\n'.format(self._nplc))
        file_handle.write("Voltage Current\n")
        file_handle.write('# minimal range {}\n'.format(self._range))
        file_handle.write("Datetime Voltage Current T3\n")
        
    def __initialize_device(self) -> None:
        """Make device ready for measurement."""
        self._device.arm()

    def __deinitialize_device(self) -> None:
        """Reset device to a safe state."""
        self._device.set_voltage(0)
        self._device.disarm()

    def __measure_data_point(self) -> Tuple[float, float]:
        """Return one data point: (voltage, current).

        Device must be initialised and armed.
        """
        return self._device.read()


