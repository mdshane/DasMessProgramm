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

from ast import literal_eval


@register('SourceMeter 2636A - I(U) - Two probe H-Field hysteresis loop')
class SMU2ProbeHSweep2636AIvU(SMU2Probe2636A):

    def __init__(self, *args, **kwargs):
        
        self._sweep_rate = kwargs.pop("sweep_rate")
        self._fields = kwargs.pop("fields")
        self._sample_name = kwargs.pop("sample_name")
        super().__init__(*args, **kwargs)

        # Initialisation of magnet controller
        self._mag = IPS120_10()
        
        # Initialise the ITC503 Temperature Controller
        self._temp = ITC(get_gpib_device(24))
        
        try:
            self._fields = literal_eval(self._fields)
        except Exception as e:
            print('ERROR', 'Malformed String for Fields', e)
            self.abort()
            return
    
        for field in self._fields:
            if not (-10 <= field <= 10): 
                print("field is too high or too low. failed field is", field, "T")
                self.abort()
                return  
            
        if not (0 <= self._sweep_rate <= 0.5): 
            print("you're insane! sweep rate is too high. (0 ... 0.3)")
            self.abort()
            return   
        
        time.sleep(1)

    
        
    @staticmethod
    def inputs() -> Dict[str, AbstractValue]:
        '''
        IPS needs additional argunemts.
        '''  
        inputs = SMU2Probe2636A.inputs()
        inputs['sweep_rate'] = FloatValue('Sweep Rate [T/min]', default=0.1)
        inputs['fields'] = StringValue('Fields', default='[0.0, 0.1, 0.2, 0.3, 0.2, 0.1, 0.0]')
        inputs['sample_name'] = StringValue('Sample Name', default='ChipXX_depNoXX')
        return inputs
      
    @staticmethod
    def outputs() -> Dict[str, AbstractValue]:
        return {'v': FloatValue('Voltage'),
                'i': FloatValue('Current'),
                'datetime': DatetimeValue('Timestamp'),
                'B': DatetimeValue('Field[T]')}

    @property
    def recommended_plots(self) -> List[PlotRecommendation]:
        field = self._mag.get_field()
        return [PlotRecommendation('Voltage Sweep', x_label='v', y_label='i', show_fit=True),
                PlotRecommendation('Magnetic Field', x_label='v', y_label='B', show_fit=False)]

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

        voltage_space = self._setup_voltage_space()

        for field in self._fields:
            if self._should_stop.is_set():
                break
                
            self._goto_field_and_stabilize(field)
            
            try:
                self._sweep_voltage(voltage_space, file_handle)
            except Exception as e:
                print('{} failed to acquire datapoint. ERROR:{}'.format(datetime.now().isoformat(), e))
                traceback.print_exc()
                

        self._deinitialize_device()

    
    def _setup_voltage_space(self):
        '''
        Voltage values:
        0 -> max-voltage
        max_voltage -> 0
        '''
        zero_to_max = np.linspace(0, self._max_voltage, self._number_of_points)
        max_to_zero = np.linspace(self._max_voltage, 0, self._number_of_points)

        return np.concatenate((zero_to_max, max_to_zero))
        

    def _sweep_voltage(self, voltage_space, file_handle):
        
        for voltage in voltage_space:
            if self._should_stop.is_set():
                print("DEBUG: Aborting measurement.")
                self._signal_interface.emit_aborted()
                break

            self._device.set_voltage(voltage)
            while True:
                try:
                    self._acquire_data_point(file_handle)
                except ValueError:
                    pass
                else:
                    break
                            


    def _goto_field_and_stabilize(self, field):
        
        self._mag.set_target_field(field)
        self._mag.set_sweep_mode(SweepMode.TO_SET_POINT)
        
        print('DEBUG', 'new set field: {}'.format(field))
        
        field_reached = False
        while not field_reached:
            current_field = self._mag.get_field()
            if abs(current_field - field) < 0.001:
                field_reached = True

            time.sleep(1)

        print('DEBUG', datetime.now().isoformat() ,'waiting 60s to settle')
        time.sleep(10)
        
 
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
        file_handle.write('# sweep rate {}\n'.format(self._sweep_rate))
        file_handle.write('# fields {}\n'.format(self._fields))

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

    

            
