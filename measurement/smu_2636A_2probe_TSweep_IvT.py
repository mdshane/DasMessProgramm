from .measurement import register, AbstractMeasurement, Contacts, PlotRecommendation
from .measurement import StringValue, FloatValue, IntegerValue, DatetimeValue, AbstractValue, SignalInterface, GPIBPathValue

from typing import Dict, Tuple, List
from typing.io import TextIO

from visa import ResourceManager
from scientificdevices.keithley.sourcemeter2400 import Sourcemeter2400
from scientificdevices.keithley.sourcemeter2602A import Sourcemeter2602A
from scientificdevices.keithley.sourcemeter2636A import Sourcemeter2636A

from scientificdevices.oxford.itc503 import ITC
from scientificdevices.oxford.ilm import ILM
from .gpib_Instrument import *

from datetime import datetime
from time import sleep, time
from threading import Event

import numpy as np
from queue import Queue

import gpib

import traceback

from ast import literal_eval


import telegram 
import configparser


@register('SourceMeter 2636A - I(T) - Two probe temperature Sweep')
class SMU2ProbeIvTBlue(AbstractMeasurement):


    def __init__(
        self, 
        signal_interface: SignalInterface,
        path: str, 
        contacts: Tuple[str, str, str, str],
        comment: str = '', 
        gpib: str='GPIB0::10::INSTR',
        sweep_rate:float = 1.0,
        temperature_end: float = 2,
        nplc: int = 3, 
        voltage:float = 0.0, 
        current_limit: float=1e-6
        ):
                     
        super().__init__(signal_interface, path, contacts)
        self._comment = comment
        self._temp = ITC(get_gpib_device(24))
        self._sweep_rate = sweep_rate
        self._voltage = voltage
        self._current_limit = current_limit
        self._gpib = gpib
        
        # Initialise the ILM Level Meter
        self._ilm =  ILM(get_gpib_device(24))
        
        # List for check of temperature convergance
        self._last_temperatures = []
            
        if not (0 <= temperature_end <= 299): 
            print("end temperature too high or too low. (0 ... 299)")
            self.abort()
            return  
            
        if not (0 <= sweep_rate <= 2.5): 
            print("you're insane! sweep rate is too high. (0 ... 2.5)")
            self.abort()
            return   
            
        resource_man = ResourceManager('@py')
        resource = resource_man.open_resource(self._gpib)
            
        self._device = SMU2ProbeIvTBlue._get_sourcemeter(resource)
        self._device.voltage_driven(0, current_limit, nplc)
            
        self._temperature_end = temperature_end
        
        self._last_toggle = time()
        
        sleep(1)

        # Setting up the bot for updates via telegram
        config = configparser.ConfigParser()
        config.read('../config.ini')
        self.telegram_bot = telegram.Bot(token= config['ALL']['TELEGRAM_TOKEN'])
        self.telegram_chat_id = config['ALL']['TELEGRAM_CHAT_ID']

    @staticmethod
    def number_of_contacts():
        return Contacts.TWO
        
    @staticmethod
    def _get_sourcemeter(resource):
        identification = resource.query('*IDN?')
        print('DEBUG', identification)
        if '2400' in identification:
            return Sourcemeter2400(resource)
        elif '2602' in identification:
            return Sourcemeter2602A(resource)
        elif '2636' in identification:
            return Sourcemeter2636A(resource)
        else:
            raise ValueError('Sourcemeter "{}" not known.'.format(identification))
            
    @staticmethod
    def inputs() -> Dict[str, AbstractValue]:
        return {'temperature_end': FloatValue('Target temperature', default=295),
                'comment': StringValue('Comment', default=''),
                'gpib': GPIBPathValue('GPIB Address', default='GPIB0::10::INSTR'),
                'voltage' : FloatValue('Voltage', default=0.0),
                'current_limit': FloatValue('Current Limit', default=1e-6),
                'sweep_rate': FloatValue('Sweep Rate [K/min]', default=1)
                }

    @staticmethod
    def outputs() -> Dict[str, AbstractValue]:
        return {'G': FloatValue('Conductance'),
                'I': FloatValue('Current'),
                'T': FloatValue('Temperature')}

    @property
    def recommended_plots(self) -> List[PlotRecommendation]:
        return [PlotRecommendation('Current Monitoring', x_label='T', y_label='I', show_fit=False),
                PlotRecommendation('Conductance Monitoring', x_label='T', y_label='G', show_fit=False)]

    def _measure(self, file_handle):
        self.__write_header(file_handle)
        self.__initialize_device()
        sleep(0.5)
        
        self._start_sweep()
        self._device.set_voltage(self._voltage)

        while not self._should_stop.is_set():
            try:
                self._acquire_data_point(file_handle)
            except:
                print('{} failed to acquire datapoint.'.format(datetime.now().isoformat()))
                traceback.print_exc()
                
            self._toggle_pid_if_necessary()
            
            if self._check_temp_reached():
                self._should_stop.set()
        
                if self._temp.T1 < 10.0:
                    print('Setpoint set to 10.0K for safety.')
                    self._temp.temperature_set_point = 10.0
                    
        
        self.__deinitialize_device()
        
        

    def _check_temp_reached(self):
        if (self._temp.T1 - self._temperature_end) > 1.0:
            return False
        else:
            maximal_list_len = 10
            stability_rate = 0.1/120 # Temperature rate below 0.1K/2min
            
            try:
                T3 = self._temp.T3
            except:
                T3 = self._temp.T3
             
            if len(self._last_temperatures) < maximal_list_len:
                self._last_temperatures.append(T3)
                return False
                
            else:
                self._last_temperatures = self._last_temperatures[-maximal_list_len:]
                self._last_temperatures.append(T3)
                
                try:
                    rate, mean = np.polyfit(np.arange(0,len(self._last_temperatures)), self._last_temperatures, 1)
                    print('DEBUG: Checking rate {0:.5f} K/min.'.format(rate))
                except:
                    print('ERROR', '-'*74)
                    traceback.print_exc()
                
                if np.abs(rate) < stability_rate:
                    print('DEBUG: Final temperature stabilized, finishing measurement.')
                    return True
                else:
                    return False


    def _start_sweep(self):
        current_temperature = self._temp.T1
        
        sweep_time = abs((current_temperature - self._temperature_end) / self._sweep_rate)
        
        self._temp.temperature_set_point = current_temperature
        self._temp.set_temperature_sweep(self._temperature_end, sweep_time = sweep_time)
        self._temp.start_temperature_sweep()
        self.send_meassage_telegram(
            bot=self.telegram_bot, 
            chat_id=self.telegram_chat_id, 
            message='Start of Sweep to `{:.2f} K`'.format(self._temperature_end), 
            send_status = True
            )

    def _toggle_pid_if_necessary(self):
        current_temperature = self._temp.T1
        
        if 20 < current_temperature < 30 and time() - self._last_toggle > 100:
            self._temp.toggle_pid_auto(False)
            sleep(0.5)
            self._temp.toggle_pid_auto(True)
    

    def _acquire_data_point(self, file_handle):
        voltage, current = self.__measure_data_point()
        T1, T2, T3 = self._temp.T1, self._temp.T2, self._temp.T3
        
        file_handle.write('{} {} {} {} {} {}\n'.format(datetime.now().isoformat(), 
                                                       voltage, current, T1, T2, T3))
        file_handle.flush()
        
        conductance = current / voltage
        
        self._signal_interface.emit_data({'G': conductance, 'I': current, 'T': T3})
        

    def __initialize_device(self) -> None:
        """Make device ready for measurement."""
        self._device.arm()        

    def __deinitialize_device(self) -> None:
        """Reset device to a safe state."""
        self._temp.stop_temperature_sweep()
        self._device.set_voltage(0)
        self._device.disarm()
        self.send_meassage_telegram(
            bot=self.telegram_bot, 
            chat_id=self.telegram_chat_id, 
            message='Measurement finished, Sweep Stopped.', 
            send_status = True
            )

    def __write_header(self, file_handle: TextIO) -> None:
        file_handle.write("# {0}\n".format(datetime.now().isoformat()))
        file_handle.write('# {}\n'.format(self._comment))
        file_handle.write('# {} V\n'.format(self._voltage))      
        file_handle.write('# {} A-max\n'.format(self._current_limit))  
        file_handle.write("# sweep rate {0} K/min\n".format(self._sweep_rate))
        file_handle.write("Datetime Voltage Current T1 T2 T3\n")

    def __measure_data_point(self):
        return self._device.read()
        
    def send_meassage_telegram(self, bot, chat_id, message, send_status = True):

        if send_status:
            device_status = self._temp.device_status
            mail_text = '*Zustand des Systems:* \n'
            mail_text += '\n'
            mail_text += '_'
            mail_text += message + '_\n'
            mail_text += '\n'
            mail_text += '`'
            mail_text += 'T_Set = {0:3.2f} K\n'.format(self._temp.temperature_set_point)
            mail_text += 'T_1   = {0:3.2f} K\n'.format(self._temp.T1)
            mail_text += 'T_2   = {0:3.2f} K\n'.format(self._temp.T2)
            mail_text += 'T_3   = {0:3.2f} K\n'.format(self._temp.T3)
            mail_text += '\n'
            mail_text += 'Heater Output = {0:2.2f} V\n'.format(self._temp.heater_output[1])
            mail_text += '                {0:2.2f} %\n'.format(self._temp.heater_output[0])
            mail_text += 'Gas Flow =      {0:2.2f} %\n'.format(self._temp.gas_flow)
            mail_text += 'Helium Level =  {0:2.2f} %\n'.format(self._ilm.level)
            mail_text += '\n'
            mail_text += 'PID Parameters:\n'
            mail_text += 'P = {0}\n'.format(self._temp.pid_parameters[0])
            mail_text += 'I = {0}\n'.format(self._temp.pid_parameters[1])
            mail_text += 'D = {0}\n'.format(self._temp.pid_parameters[2])
            mail_text += '\n'
            mail_text += 'Auto PID      = {}\n'.format(str(bool(device_status['auto_pid'])))
            mail_text += 'Sweep Running = {}\n'.format(str(bool(device_status['sweep_running'])))
            mail_text += 'Sweep Holding = {}\n'.format(str(bool(device_status['sweep_holding'])))
            mail_text += '`'
        else:
            mail_text = message
        
        bot.send_message(chat_id=chat_id, text=mail_text, parse_mode=telegram.ParseMode.MARKDOWN)

