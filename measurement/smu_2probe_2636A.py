from .measurement import register, AbstractMeasurement, Contacts, PlotRecommendation
from .measurement import StringValue, FloatValue, IntegerValue, DatetimeValue, AbstractValue, SignalInterface, GPIBPathValue

import numpy as np
from datetime import datetime
from threading import Event
import time
from typing import Dict, Tuple, List
from typing.io import TextIO

from .smu_2probe import SMU2Probe

@register('SourceMeter two probe voltage sweep 2636A')
class SMU2Probe2636A(SMU2Probe):
    """Voltage driven 2-probe current measurement on a sourcemeter."""


    def __init__(self, *args, **kwargs) -> None:
        self._range = kwargs.pop("min_range") # TODO
        super().__init__(*args, **kwargs)
        self._device.voltage_driven(0, self._current_limit, self._nplc, range=self._range)


    @staticmethod
    def inputs() -> Dict[str, AbstractValue]:
        '''
        SMU 2636A needs additional setting for minimal measurement range.
        '''  
        inputs = SMU2Probe.inputs()
        inputs['min_range'] = FloatValue('Minimal Range', default=1e-8)
        return inputs


    @property
    def recommended_plots(self) -> List[PlotRecommendation]:
        return [PlotRecommendation('Voltage Sweep', x_label='v', y_label='i', show_fit=True)]


    def _measure(self, file_handle) -> None:
        """Custom measurement code lives here.
        """
        self._write_header(file_handle)
        self._initialize_device()
        time.sleep(0.5)

        for voltage in np.linspace(0, self._max_voltage, self._number_of_points):
            if self._should_stop.is_set():
                print("DEBUG: Aborting measurement.")
                self._signal_interface.emit_aborted()
                break

            self._device.set_voltage(voltage)
            voltage, current = self._measure_data_point()
            file_handle.write("{} {}\n".format(voltage, current))
            file_handle.flush()
            # Send data point to UI for plotting:
            self._signal_interface.emit_data({'v': voltage, 'i': current, 'datetime': datetime.now()})

        self._deinitialize_device()


    def _write_header(self, file_handle: TextIO) -> None:
        """Write a file header for present settings.

        Arguments:
            file_handle: The open file to write to
        """
        file_handle.write("# {0}\n".format(datetime.now().isoformat()))
        file_handle.write('# {}\n'.format(self._comment))
        file_handle.write("# maximum voltage {0} V\n".format(self._max_voltage))
        file_handle.write("# current limit {0} A\n".format(self._current_limit))
        file_handle.write('# nplc {}\n'.format(self._nplc))
        file_handle.write('# minimal range {}\n'.format(self._range))
        
    def _initialize_device(self) -> None:
        """Make device ready for measurement."""
        self._device.arm()

    def _deinitialize_device(self) -> None:
        """Reset device to a safe state."""
        self._device.set_voltage(0)
        self._device.disarm()

    def _measure_data_point(self) -> Tuple[float, float]:
        """Return one data point: (voltage, current).

        Device must be initialised and armed.
        """
        return self._device.read()
