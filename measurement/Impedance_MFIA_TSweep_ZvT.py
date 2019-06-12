from .measurement import register, AbstractMeasurement, Contacts, PlotRecommendation
from .measurement import StringValue, FloatValue, IntegerValue, DatetimeValue, AbstractValue, SignalInterface, GPIBPathValue

from typing import Dict, Tuple, List
from typing.io import TextIO

import zhinst.utils

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


@register('MFIA - Z(T) - Two probe temperature Sweep')
class Impedance_MFIA_TSweep_ZvT(AbstractMeasurement):


    def __init__(
        self, 
        signal_interface: SignalInterface,
        path: str, 
        contacts: Tuple[str, str, str, str],
        comment: str = '', 
        sweep_rate:float = 1.0,
        temperature_end: float = 2,
        test_signal: float = 0.1,
        frequencies = [10.0, 100, 1000, 10000, 100000, 1000000],
        plot_freq: int = 0,
        sleep_between_measurements: float = 1.0
        ):
                     
        super().__init__(signal_interface, path, contacts)
        self._comment = comment

        # Initialise the ITC Temperature Controller 
        self._temp = ITC(get_gpib_device(24))
        self._sweep_rate = sweep_rate
        
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
                        
        self._temperature_end = temperature_end
        self._last_toggle = time()

        # Initialise the ILM Level Meter
        self._ilm =  ILM(get_gpib_device(24))
        
        # Initialise the the MFIA
        self._frequencies = frequencies
        self._test_signal = test_signal
        self.plot_freq = plot_freq
        self.sleep_between_measurements = sleep_between_measurements
        auto_output = 0

        settings = {
            "awg": {},
            "data_acquisition": {
                "demods": {
                    "0": {},
                    "1": {}
                },
                "imps": {
                    "0": {
                        'auto': {
                            'output': auto_output,
                            'inputrange': 1,
                            },
                        'output': {
                            'amplitude': self._test_signal
                            },
                        'freq': 1000,
                        'enable': 1,
                        'mode': 0,
                        'calib': {
                            'user': {
                                'enable': 0
                                }
                            },
                        'confidence': {
                            'compensation': {
                                'enable': 1
                                },
                            'opendetect': {
                                'enable': 1
                                },
                            'overflow': {
                                'enable': 1
                                },
                            'suppression': {
                                'enable': 1
                                },
                            'underflow': {
                                'enable': 1
                                }
                            }
                        }
                    },
                'sigouts':{
                    '0': {
                        'enables': {
                            '1': 1
                            }
                        }
                    }
            },
            "impedance": {
                'filename': 'Probenstab_SOL_Compensation_MFITF_HighPrecision',
                'load': 1
                },
            "multi_device": {},
            "pid": {},
            "recorder": {},
            "sweeper": {}
        }

        self.device_id = 'dev3964'
        self.device = MFIADevice(device_id=self.device_id)
        self.mfia_measurement = ContinuousImpedanceMeasurement(
            sample_name='Test',
            device=self.device,
            device_id=self.device_id,
            settings=settings,
            file_base_path=path,
            comment=self._comment)


        sleep(1)

        # Setting up the bot for updates via telegram
        config = configparser.ConfigParser()
        config.read('../config.ini')
        self.telegram_bot = telegram.Bot(token= config['ALL']['TELEGRAM_TOKEN'])
        self.telegram_chat_id = config['ALL']['TELEGRAM_CHAT_ID']


    @staticmethod
    def inputs() -> Dict[str, AbstractValue]:
        return {'comment': StringValue('Comment', default=''),
                'sweep_rate': FloatValue('Sweep Rate [K/min]', default=1),
                'temperature_end': FloatValue('Target temperature', default=295),                
                'test_signal' : FloatValue('Test Signal [V]', default=0.0),
                'frequencies': FloatValue('Frequency List [Hz]', default=[100, 1000, 10000, 100000, 1000000]),
                'plot_freq': IntegerValue('Plot Frequency No.'), default=0),
                'sleep_between_measurements': FloatValue('Sleep Time between individual measurements [s]'), default=1.0)
                }

    
    @staticmethod
    def outputs() -> Dict[str, AbstractValue]:
        return {'f': FloatValue('f'),
                'AbsZ': FloatValue('AbsZ'),
                'Phase': FloatValue('AbsZ'),
                'T': FloatValue('Temperature')
                }

    
    @staticmethod
    def number_of_contacts() -> Contacts:
        return Contacts.NONE

    @property
    def recommended_plots(self) -> List[PlotRecommendation]:
        return [PlotRecommendation('Impedance Monitoring', x_label='T', y_label='AbsZ', show_fit=False)]

    def _measure(self):

        self._start_sweep()

        self.mfia_measurement.start_auto_ranging()

        while not self._should_stop.is_set():
            try:
                self._acquire_data_point()
            except:
                print('{} failed to acquire datapoint.'.format(datetime.now().isoformat()))
                traceback.print_exc()
                
            self._toggle_pid_if_necessary()
            
            if self._check_temp_reached():
                self._should_stop.set()
        
                if self._temp.T1 < 20.0:
                    print('Setpoint set to 5.0K for safety.')
                    self._temp.temperature_set_point = 15.0

            sleep(self.sleep_between_measurements)        
        
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
    

    def _acquire_data_point(self):
        data_list = self.mfia_measurement.measure(self._frequencies)
        data = data_list[self.plot_freq]
             
        self._signal_interface.emit_data({
            'f': data['f'],
            'AbsZ': data['AbsZ'], 
            'Phase': data['Phase'], 
            'T': data['T3']
            })

    def __initialize_device(self) -> None:
        """Make device ready for measurement."""
        pass  

    def __deinitialize_device(self) -> None:
        """Reset device to a safe state."""
        self._temp.stop_temperature_sweep()
        
        self.mfia_measurement.finalize_measurement()
        self.send_meassage_telegram(
            bot=self.telegram_bot, 
            chat_id=self.telegram_chat_id, 
            message='Measurement finished, Sweep Stopped.', 
            send_status = True
            )

    def __write_header(self, file_handle: TextIO) -> None:
        pass

        
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

