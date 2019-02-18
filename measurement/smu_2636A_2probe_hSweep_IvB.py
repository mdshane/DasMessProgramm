from .measurement import register, AbstractMeasurement, Contacts, PlotRecommendation
from .measurement import StringValue, FloatValue, IntegerValue, DatetimeValue, AbstractValue, SignalInterface, GPIBPathValue
from .gpib_Instrument import *


import numpy as np
from datetime import datetime
from threading import Event
import time
from typing import Dict, Tuple, List
from typing.io import TextIO

from .smu_2636A_2probe_IvU import SMU2Probe2636A
from scientificdevices.oxford.ips120 import IPS120_10, ControlMode, CommunicationProtocol, SweepMode, SwitchHeaterMode
from scientificdevices.oxford.itc503 import ITC
import gpib

import traceback

from ast import literal_eval
from enum import Enum


@register('SourceMeter 2636A - I(B) - Two probe H-Field hysteresis loop')
class SMU2ProbeHSweep2636AIvB(SMU2Probe2636A):

    class State(Enum):
        START = 0
        GOING_UP = 1
        HALTING_UP = 2
        GOING_DOWN = 3
        HALTING_DOWN = 4
        GOING_BACK_UP = 5
        HALTING_UP2 = 6
        GOING_ZERO = 7
        DONE = 8

    def __init__(self, *args, **kwargs):
        
        self._sweep_rate = kwargs.pop("sweep_rate")
        self._sample_name = kwargs.pop("sample_name")
        self._max_field = kwargs.pop('max_field')
        super().__init__(*args, **kwargs)
        
        # Initialise the ITC503 Temperature Controller
        self._temp = ITC(get_gpib_device(24))

        # Initialisation of magnet controller
        self._mag = IPS120_10()
        self._state = self.State.START
        self._last_field = 0.0
        
        if not (0 <= self._max_field <= 8): 
            print("field is too high or too low. (0 ... 8)")
            self.abort()
            return  
            
        if not (0 <= self._sweep_rate <= 1.0): 
            print("you're insane! sweep rate is too high. (0 ... 1.0)")
            self.abort()
            return   
        
        time.sleep(1)

    
        
    @staticmethod
    def inputs() -> Dict[str, AbstractValue]:
        '''
        IPS needs additional argunemts.
        '''  
        inputs = SMU2Probe2636A.inputs()
        inputs.pop('n')
        inputs['sweep_rate'] = FloatValue('Sweep Rate [T/min]', default=0.1)
        inputs['max_field'] = FloatValue('Max Field [T]', default=1)
        inputs['sample_name'] = StringValue('Sample Name', default='ChipXX_depNoXX')
        return inputs
      
    @staticmethod
    def outputs() -> Dict[str, AbstractValue]:
        return {'v': FloatValue('Voltage'),
                'i': FloatValue('Current'),
                'B': DatetimeValue('Field[T]')}

    @property
    def recommended_plots(self) -> List[PlotRecommendation]:
        return [PlotRecommendation('Field Sweep', x_label='B', y_label='i', show_fit=False)]

    def _generate_file_name_prefix(self) -> str:
        return '{}_hSweepHysteresis_contacts_{}_'.format(self._sample_name, '--'.join(self._contacts))

    def _generate_plot_file_name_prefix(self, pair) -> str:
        return '{}_hSweepHysteresis_contacts_{}_plot-{}-{}_'.format(self._sample_name, '--'.join(self._contacts), pair[0], pair[1])
        
        
    def _measure(self, file_handle) -> None:
        """
        Custom measurement code lives here.
        """
        self._write_header(file_handle)
        self._initialize_device()
        time.sleep(0.5)
        
        self._device.set_voltage(self._max_voltage)
    
        while not self._should_stop.is_set():
            try:
                self._acquire_data_point(file_handle)
            except Exception as e:
                print('{} failed to acquire datapoint. ERROR:{}'.format(datetime.now().isoformat(), e))
                traceback.print_exc()
                
            self._switch_states_if_necessary()

        self._deinitialize_device()



    def _switch_states_if_necessary(self):
        try:
            field = self._mag.get_field()
        except Exception as e:
            print('{} failed to acquire datapoint. ERROR:{}'.format(datetime.now().isoformat(), e))
            traceback.print_exc()
        
        if not field:
            field = self._last_field
            
        self._last_field = field
        
        
        if self._state == self.State.START:
            self._mag.set_target_field(self._max_field)
            self._mag.set_sweep_mode(SweepMode.TO_SET_POINT)
            self._state = self.State.GOING_UP
        elif self._state == self.State.GOING_UP:
            if abs(field - self._max_field) < 0.001:
                time.sleep(2)
                self._state = self.State.HALTING_UP
                self._mag.set_sweep_mode(SweepMode.HOLD)
        elif self._state == self.State.HALTING_UP:
            self._mag.set_target_field(-self._max_field)
            self._state = self.State.GOING_DOWN
            self._mag.set_sweep_mode(SweepMode.TO_SET_POINT)
        elif self._state == self.State.GOING_DOWN:
            if abs(field - (-self._max_field)) < 0.001:
                time.sleep(2)
                self._state = self.State.HALTING_DOWN
                self._mag.set_sweep_mode(SweepMode.HOLD)
        elif self._state == self.State.HALTING_DOWN:
            self._mag.set_target_field(self._max_field)
            self._mag.set_sweep_mode(SweepMode.TO_SET_POINT)
            self._state = self.State.GOING_BACK_UP
        elif self._state == self.State.GOING_BACK_UP:
            if abs(field - self._max_field) < 0.001:
                time.sleep(2)
                self._state = self.State.HALTING_UP2
                self._mag.set_sweep_mode(SweepMode.HOLD)
        elif self._state == self.State.HALTING_UP2:
            self._mag.set_target_field(0)
            self._mag.set_sweep_mode(SweepMode.TO_ZERO)
            self._state = self.State.GOING_ZERO
        elif self._state == self.State.GOING_ZERO:
            if abs(field) < 0.001:
                time.sleep(2)
                self._state = self.State.DONE
                self._mag.set_sweep_mode(SweepMode.HOLD)
        else:
            self._should_stop.set()
            self._mag.set_sweep_mode(SweepMode.HOLD)
                                              
 
    def _acquire_data_point(self, file_handle):
        meas_voltage, meas_current = self._measure_data_point()
        T1, T2, T3 = self._temp.T1, self._temp.T2, self._temp.T3
        field = self._mag.get_field()
        
        file_handle.write('{} {} {} {} {} {} {} \n'.format(datetime.now().isoformat(), field, 
                                                             meas_voltage, meas_current, T1, T2, T3))
        file_handle.flush()
        
        self._signal_interface.emit_data({'v': meas_voltage, 'i': meas_current, 'datetime': datetime.now(), 'B': field})




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
        file_handle.write('# max-field {} T\n'.format(self._max_field))
        file_handle.write("# sweep rate {0} T/min\n".format(self._sweep_rate))

        file_handle.write("Datetime Field Voltage Current T1 T2 T3\n")

    def _initialize_device(self):
        self._device.arm()

        self._mag.clear()
        self._mag.set_control_mode(ControlMode.REMOTE_AND_UNLOCKED)
        self._mag.set_communication_protocol(CommunicationProtocol.EXTENDED_RESOLUTION)
        self._mag.set_sweep_mode(SweepMode.HOLD)
        self._mag.set_switch_heater(SwitchHeaterMode.ON)
        self._mag.set_field_sweep_rate(self._sweep_rate)
        
    def _deinitialize_device(self) -> None:
        self._device.set_voltage(0)
        self._device.disarm()

        self._mag.set_target_field(0)
        self._mag.set_sweep_mode(SweepMode.TO_ZERO)
        
        field = self._mag.get_field()
        while abs(field) >= 0.001:
            time.sleep(1)
            field = self._mag.get_field()
            
        self._mag.set_sweep_mode(SweepMode.HOLD)

        

    

            
