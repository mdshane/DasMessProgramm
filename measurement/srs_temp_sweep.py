from .measurement import register, AbstractMeasurement, Contacts, PlotRecommendation
from .measurement import StringValue, FloatValue, IntegerValue, DatetimeValue, AbstractValue, SignalInterface, GPIBPathValue

from typing import Dict, Tuple, List
from typing.io import TextIO

from visa import ResourceManager
from scientificdevices.stanford_research_systems.sr830m import SR830m

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


@register('SRS830 Resistance vs. Temp. (blue)')
class SRS830RvTBlue(AbstractMeasurement):


    def __init__(self, signal_interface: SignalInterface,
                 path: str, contacts: Tuple[str, str, str, str],
                 R: float = 9.99e6, comment: str = '', gpib: str='GPIB0::7::INSTR',
                 sweep_rate:float = 1.0,
                 temperature_end: float = 2):
                     
        super().__init__(signal_interface, path, contacts)
        self._comment = comment
        self._device = SR830m(gpib)
        self._temp = ITC(get_gpib_device(24))
        self._pre_resistance = R
        self._sweep_rate = sweep_rate
            
        if not (0 <= temperature_end <= 295): 
            print("end temperature too high or too low. (0 ... 295)")
            self.abort()
            return  
            
        if not (0 <= sweep_rate <= 2.5): 
            print("you're insane! sweep rate is too high. (0 ... 2.5)")
            self.abort()
            return   
            
        # temperature control related variables
        self._temperature_end = temperature_end
        self._last_toggle = time()
        
        # List for check of temperature convergance
        self._last_temperatures = []

        # Lock-In control related variables
        self._sens_check_time = time()
        
        # Initialise the ILM Level Meter
        self._ilm =  ILM(get_gpib_device(24))
        
        
        # Setting up the bot for updates via telegram
        config = configparser.ConfigParser()
        config.read('../config.ini')
        self.telegram_bot = telegram.Bot(token= config['ALL']['TELEGRAM_TOKEN'])
        self.telegram_chat_id = config['ALL']['TELEGRAM_CHAT_ID']
        
        sleep(1)

    @staticmethod
    def number_of_contacts():
        return Contacts.FOUR

    @staticmethod
    def inputs() -> Dict[str, AbstractValue]:
        return {'R': FloatValue('Pre Resistance', default=9.99e6),
                'temperature_end': FloatValue('Target temperature', default=295),
                'comment': StringValue('Comment', default=''),
                'sweep_rate': FloatValue('Sweep Rate', default = 1.0),
                'gpib': GPIBPathValue('GPIB Address', default='GPIB0::7::INSTR'),
                }

    @staticmethod
    def outputs() -> Dict[str, AbstractValue]:
        return {'R': FloatValue('Resistance'),
                'T': DatetimeValue('Temperature')}

    @property
    def recommended_plots(self) -> List[PlotRecommendation]:
        return [PlotRecommendation('Resistance Monitoring', x_label='T', y_label='R', show_fit=False)]

    def _measure(self, file_handle):
        self.__write_header(file_handle)
        sleep(0.5)
        
        self._start_sweep()

        while not self._should_stop.is_set():
            try:
                self._acquire_data_point(file_handle)
            except:
                print('{} failed to acquire datapoint.'.format(datetime.now().isoformat()))
                traceback.print_exc()
                
            self._toggle_pid_if_necessary()
            
            if self._check_temp_reached():
                self._should_stop.set()
        
                if self._temp.T1 < 20.0:
                    print('Setpoint set to 5.0K for safety.')
                    self._temp.temperature_set_point = 15.0

        self.__deinitialize_device()

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
        x, y, r, t = self.__measure_data_point()
        sensitivity = self.__get_auxiliary_data()
        T1, T2, T3 = self._temp.T1, self._temp.T2, self._temp.T3
        
        file_handle.write('{} {} {} {} {} {} {} {} {}\n'.format(datetime.now().isoformat(), 
                                                             x, y, r, t,sensitivity, T1, T2, T3))
        file_handle.flush()
        
        resistance = x / self._device.slvl * self._pre_resistance
        
        self._signal_interface.emit_data({'R': resistance, 'T': T3})

        self._check_sensitivitiy(r, sensitivity)

    def _check_sensitivitiy(self, r, sensitivity):
        sensitivity_list = [2E-9, 5E-9, 10E-9, 20E-9, 50E-9, 100E-9, 200E-9, 500E-9, 1E-6, 2E-6, 5E-6, 10E-6, 20E-6,
                            50E-6, 100E-6, 200E-6, 500E-6, 1E-3, 2E-3, 5E-3, 10E-3, 20E-3, 50E-3, 100E-3, 200E-3,
                            500E-3, 1]
                            
        if time() > self._sens_check_time + 10:
            sensitivity_range_max = sensitivity_list[int(sensitivity)]
            if r > 0.9 * sensitivity_range_max:
                self._device.sens = int(sensitivity) + 1
                print('DEBUG: Sensitivity updated.')
            elif r < 0.25 * sensitivity_range_max:
                self._device.sens =  int(sensitivity) - 1
                print('DEBUG: Sensitivity updated.')
            self._sens_check_time = time()
        
    
    def _check_temp_reached(self):
        if np.abs(self._temp.T1 - self._temperature_end) > 1.0:
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

    def __deinitialize_device(self) -> None:
        self._temp.stop_temperature_sweep()
        self.send_meassage_telegram(
            bot=self.telegram_bot, 
            chat_id=self.telegram_chat_id, 
            message='Measurement finished, Sweep Stopped.', 
            send_status = True
            )

    def __write_header(self, file_handle: TextIO) -> None:
        file_handle.write("# {0}\n".format(datetime.now().isoformat()))
        file_handle.write('# {}\n'.format(self._comment))
        file_handle.write('# {} Hz\n'.format(self._device.freq))
        file_handle.write('# {} V\n'.format(self._device.slvl))        
        file_handle.write('# {} Time constant\n'.format(self._device.oflt))
        file_handle.write("# pre resistance {0} OHM\n".format(self._pre_resistance))
        file_handle.write("# sweep rate {0} K/min\n".format(self._sweep_rate))
        file_handle.write("Datetime Real Imaginary Amplitude Theta Sensitivity T1 T2 T3\n")

    def __measure_data_point(self):
        return (self._device.outpX, self._device.outpY, self._device.outpR, self._device.outpT)

    def __get_auxiliary_data(self):
        return self._device.sens
        
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
