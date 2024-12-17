import qcodes as qc
from sqdtoolz.ExperimentConfiguration import*
from sqdtoolz.Variable import*
from sqdtoolz.ExperimentSpecification import*
from sqdtoolz.HAL.HALbase import*
from sqdtoolz.HAL.ACQ import*
from sqdtoolz.HAL.AWG import*
from sqdtoolz.HAL.DDG import*
from sqdtoolz.HAL.GENmwSource import*
from sqdtoolz.HAL.GENvoltSource import*
from sqdtoolz.HAL.GENswitch import*
from sqdtoolz.HAL.GENswitchTrig import*
from sqdtoolz.HAL.ACQvna import*
from sqdtoolz.HAL.GENatten import*
from sqdtoolz.HAL.GENsmu import*
from sqdtoolz.HAL.Processors.ProcessorCPU import*
try:
    from sqdtoolz.HAL.Processors.ProcessorGPU import*
except (ModuleNotFoundError, ImportError):
    pass
from sqdtoolz.HAL.Processors.ProcessorFPGA import*
from datetime import datetime
from pathlib import Path
import json
import os
import time
import numpy as np
import sys

class customJSONencoder(json.JSONEncoder):
    def default(self, obj):
        #Inspired by: https://stackoverflow.com/questions/56250514/how-to-tackle-with-error-object-of-type-int32-is-not-json-serializable/56254172
        if isinstance(obj, np.int32):
            return int(obj)
        if isinstance(obj, np.int64):
            return int(obj)
        return json.JSONEncoder.default(self, obj)

class Laboratory:
    def __init__(self, instr_config_file, save_dir, using_VS_Code=False):
        if instr_config_file == "":
            self._station = qc.Station()
        else:
            self._station = qc.Station(config_file=instr_config_file)
        self._instr_config_file = instr_config_file

        #Convert Windows backslashes into forward slashes (should be compatible with MAC/Linux then...)
        self._save_dir = save_dir.replace('\\','/')
        if self._save_dir[-1] != '/':
            self._save_dir += '/'
            print("Warning: Added a slash / to the end of the path; the save-directory must be a valid path.")
        self._group_dir = {'Dir':"", 'InitDir':"", 'SweepQueue':[], 'ExptIndex' : -1}

        Path(self._save_dir).mkdir(parents=True, exist_ok=True)

        self._using_VS_Code = using_VS_Code
        self._cur_message = ''

        self._hal_objs = {}
        self._processors = {}
        self._expt_configs = {}
        self._variables = {}
        self._specifications = {}
        self._waveform_transforms = {}
        self._activated_instruments = []
        self._update_state = True

    @property
    def UpdateStateEnabled(self):
        return self._update_state
    @UpdateStateEnabled.setter
    def UpdateStateEnabled(self, bool_val):
        self._update_state = bool_val

    def reload_yaml(self):
        #NOTE: This will update the snapshots and thus, change instrument state of already loaded instruments. But it is handy
        #to help load a new instrument into the QCoDeS station (when adding a new instrument in the YAML).
        if self._instr_config_file != "":
            self._station.load_config_file(self._instr_config_file)

    def _load_json_file(self, filepath):
        if os.path.isfile(filepath):
            with open(filepath) as json_file:
                data = json.load(json_file)
                return data
            return None
        return None

    def update_variables_from_last_expt(self, file_name = ''):
        if file_name == '':
            #TODO: Stress test this with say 100000 directories
            dirs = [x[0] for x in os.walk(self._save_dir)]  #Walk gives a tuple: (dirpath, dirnames, filenames)
            cur_dir = dirs[-1].replace('\\','/')
            if os.path.isfile(cur_dir + "/laboratory_parameters.txt"):
                filepath = cur_dir + "/laboratory_parameters.txt"
        else:
            filepath = file_name
        with open(filepath) as json_file:
            data = json.load(json_file)
            for cur_key in data.keys():
                cur_dict = data[cur_key]
                if cur_key in self._variables.keys():
                    self._variables[cur_key]._set_current_config(cur_dict)
                else:
                    self._variables[cur_key] = globals()[cur_dict['Type']].fromConfigDict(cur_key, cur_dict, self)

    def cold_reload_last_configuration(self, folder_dir = ""):
        if folder_dir == "" and os.path.isfile(self._save_dir + "_last_state.txt") and os.path.isfile(self._save_dir + "_last_vars.txt") and os.path.isfile(self._save_dir + "_last_exp_configs.txt"):
            self.cold_reload_labconfig(self._load_json_file(self._save_dir + "_last_state.txt"))
            self._print_message(f"Loading all Experiment Configurations")
            self.cold_reload_experiment_configurations(self._load_json_file(self._save_dir + "_last_exp_configs.txt"))
            self._erase_line()
            self._print_message(f"Loading all Variables")
            self.update_variables_from_last_expt(self._save_dir + "_last_vars.txt")
            self._erase_line()
            self._print_message(f"Cold Reload Complete")
            #Don't need to run update_state as it's already there!
        else:
            if folder_dir != "":
                dirs = [folder_dir]
            else:
                #Go through the directories in reverse chronological order (presuming data-stamped folders)
                dirs = [x[0] for x in os.walk(self._save_dir)]  #Walk gives a tuple: (dirpath, dirnames, filenames)
                dirs.sort()

            for cur_cand_dir in dirs[::-1]:
                cur_dir = cur_cand_dir.replace('\\','/')
                #Check current candidate directory has the required files
                if not os.path.isfile(cur_dir + "/laboratory_configuration.txt"):
                    continue
                if not os.path.isfile(cur_dir + "/experiment_configurations.txt"):
                    continue
                if not os.path.isfile(cur_dir + "/laboratory_parameters.txt"):
                    continue
                #If the files concurrently exist, then load the data...
                self.cold_reload_labconfig(self._load_json_file(cur_dir + "/laboratory_configuration.txt"))
                self.cold_reload_experiment_configurations(self._load_json_file(cur_dir + "/experiment_configurations.txt"))
                self.update_variables_from_last_expt(cur_dir + "/laboratory_parameters.txt")
                self.update_state()
                return
            assert False, "No valid previous experiment with all data files were found to be present."

    def cold_reload_experiment_configurations(self, config_dict):
        for cur_expt_config in config_dict:
            cur_keys = config_dict[cur_expt_config]['HALs']
            cur_types = [x['Type'] for x in cur_keys]
            cur_hals = [x['Name'] for x in cur_keys]
            #Find the ACQ HAL module...
            acq_hal = None
            for cur_hal in cur_hals:
                if self.HAL(cur_hal).IsACQhal:
                    acq_hal = cur_hal
                    break
            new_expt_config = ExperimentConfiguration(cur_expt_config, self, 0, cur_hals, acq_hal)
            new_expt_config.update_config(config_dict[cur_expt_config], False)
    
    def _print_message(self, message):
        self._cur_message = message
        print(self._cur_message, end='\r')
    def _erase_line(self):
        print(" " * len(self._cur_message), end='\r')

    def cold_reload_labconfig(self, config_dict):
        for cur_instr in config_dict['ActiveInstruments']:
            self._print_message(f"Loading QCoDeS Instrument: {cur_instr}")
            self.load_instrument(cur_instr)
            self._erase_line()
        #Create the HALs
        for dict_cur_hal in config_dict['HALs']:
            self._print_message(f"Loading HAL: {dict_cur_hal['Name']}")
            cur_class_name = dict_cur_hal['Type']
            globals()[cur_class_name].fromConfigDict(dict_cur_hal, self)
            self._erase_line()
        #Create and load the PROCs
        for dict_cur_proc in config_dict['PROCs']:
            self._print_message(f"Loading PROC: {dict_cur_proc['Name']}")
            cur_class_name = dict_cur_proc['Type']
            globals()[cur_class_name].fromConfigDict(dict_cur_proc, self)
            self._erase_line()
        #Create and load the WFMTs
        for dict_cur_wfmt in config_dict['WFMTs']:
            self._print_message(f"Loading WFMT: {dict_cur_wfmt['Name']}")
            cur_class_name = dict_cur_wfmt['Type']
            globals()[cur_class_name].fromConfigDict(dict_cur_wfmt, self)
            self._erase_line()
        #Create and load the SPECs
        for dict_cur_spec in config_dict['SPECs']:
            self._print_message(f"Loading SPEC: {dict_cur_spec['Name']}")
            ExperimentSpecification(dict_cur_spec["Name"], self)._set_current_config(dict_cur_spec)
            self._erase_line()
        #Load parameters (including trigger relationships) onto the HALs
        for dict_cur_hal in config_dict['HALs']:
            cur_hal_name = dict_cur_hal['Name']
            self._hal_objs[cur_hal_name]._set_current_config(dict_cur_hal, self)

    def makesafe_HALs(self):
        for cur_hal in self._hal_objs:
            if not self.HAL(cur_hal).ManualActivation:
                self.HAL(cur_hal).deactivate()

    def _resolve_sqdobj_tree(self, sqdObj):
        resolution_tree = []
        if sqdObj == None:
            return []
        cur_obj = sqdObj
        cur_parent = cur_obj.Parent  #Note that Parent is: (object reference to parent, metadata to find current object from parent object's POV)
        while (type(cur_parent) is tuple and cur_parent[0] != None):
            resolution_tree += [( cur_obj.Name, cur_parent[1] )]
            cur_obj = cur_parent[0]
            cur_parent = cur_obj.Parent
        if isinstance(cur_obj, HALbase):
            assert cur_obj.Name in self._hal_objs, f"It seems that {sqdObj.Name} is a part of some rogue unregistered HAL object."
            resolution_tree += [(cur_obj.Name, 'HAL')]
        elif isinstance(cur_obj, WaveformTransformation):
            assert cur_obj.Name in self._waveform_transforms, f"It seems that {sqdObj.Name} is a part of some rogue unregistered WaveformTransformation object."
            resolution_tree += [(cur_obj.Name, 'WFMT')]
        elif isinstance(cur_obj, VariableBase):
            assert cur_obj.Name in self._variables, f"It seems that {sqdObj.Name} is a part of some rogue unregistered Variable object."
            resolution_tree += [(cur_obj.Name, 'VAR')]
        return resolution_tree[::-1]

    def _get_resolved_obj(self, res_list):
        if len(res_list) == 0:
            return None

        ret_obj = None
        if res_list[0][1] == 'HAL':
            ret_obj = self.HAL(res_list[0][0])
        elif res_list[0][1] == 'WFMT':
            ret_obj = self.WFMT(res_list[0][0])
        elif res_list[0][1] == 'VAR':
            ret_obj = self.VAR(res_list[0][0])
        #
        if ret_obj == None:
            return None
        
        if len(res_list) > 0:
            if ret_obj == None:
                return None
            for m in range(1,len(res_list)):
                if ret_obj == None:
                    return None
                ret_obj = ret_obj._get_child(res_list[m])
        return ret_obj

    def _HAL_exists(self, hal_name):
        return hal_name in self._hal_objs
    def _register_HAL(self, hal_obj):
        if not (hal_obj.Name in self._hal_objs):
            self._hal_objs[hal_obj.Name] = hal_obj
            return True
        return False
    def HAL(self, hal_ID, disable_warning=False):
        if hal_ID in self._hal_objs:
            return self._hal_objs[hal_ID]
        else:
            if not disable_warning:
                print(f'Warning: HAL {hal_ID} has not been initialised!')
            return None

    def _register_PROC(self, proc):
        if not (proc.Name in self._processors):
            self._processors[proc.Name] = proc
            return True
        return False
    def PROC(self, proc_name, disable_warning=False):
        if proc_name in self._processors:
            return self._processors[proc_name]
        else:
            if not disable_warning:
                print(f'Warning: PROC {proc_name} has not been initialised!')
            return None

    def _register_WFMT(self, wfmt):
        if not (wfmt.Name in self._waveform_transforms):
            self._waveform_transforms[wfmt.Name] = wfmt
            return True
        return False
    def WFMT(self, wfmt_name, disable_warning=False):
        if wfmt_name in self._waveform_transforms:
            return self._waveform_transforms[wfmt_name]
        else:
            if not disable_warning:
                print(f'Warning: WFMT {wfmt_name} has not been initialised!')
            return None

    def _register_VAR(self, hal_var):
        if not (hal_var.Name in self._variables):
            self._variables[hal_var.Name] = hal_var
            return True
        return False
    def VAR(self, param_name, disable_warning=False):
        if param_name in self._variables:
            return self._variables[param_name]
        else:
            if not disable_warning:
                print(f'Warning: VAR {param_name} has not been initialised!')
            return None

    def _register_SPEC(self, spec):
        if not (spec.Name in self._specifications):
            self._specifications[spec.Name] = spec
            return True
        return False
    def SPEC(self, spec_name, disable_warning=False):
        if spec_name in self._specifications:
            return self._specifications[spec_name]
        else:
            if not disable_warning:
                print(f'Warning: SPEC {spec_name} has not been initialised!')
            return None

    def _register_CONFIG(self, expt_config):
        if not (expt_config.Name in self._expt_configs):
            self._expt_configs[expt_config.Name] = expt_config
            return True
        return False
    def CONFIG(self, expt_config_name, disable_warning=False):
        if expt_config_name in self._expt_configs:
            return self._expt_configs[expt_config_name]
        else:
            if not disable_warning:
                print(f'Warning: CONFIG {expt_config_name} has not been initialised!')
            return None

    def add_instrument(self, instrObj):
        self._station.add_component(instrObj)
        self._activated_instruments += [instrObj.name]

    def load_instrument(self, instrID):
        # assert not (instrID in self._station.components), f"Instrument by the name {instrID} has already been loaded."
        if not (instrID in self._activated_instruments):
            #Check if the instrument is in the station, but unregistered (i.e. it crashed during initialisation). If so, remove it...
            #ALSO NOTE:
            #   QCoDeS does this awful thing where it stores the instruments inside the Instrument class attribute - i.e. one cannot run
            #   multiple QCoDeS instances at once in a given kernel! Anyway, it stores its own list of instruments that may not appear in
            #   components if initialisation fails...
            if instrID in qc.Instrument._all_instruments:
                instr = qc.Instrument.find_instrument(instrID)
                instr.close()
            self._station.load_instrument(instrID)
            self._activated_instruments += [instrID]
    
    def release_all_instruments(self):
        self._station.close_all_registered_instruments()

    def _get_instrument(self, instrID):
        if type(instrID) is list:
            assert instrID[0] in self._station.components, f"Instrument by the name {instrID[0]} has not been loaded. Call load_instrument on it first."
            cur_instr_obj = self._station.components[instrID[0]]
            #Go through each submodule...
            for m in range(1, len(instrID)):
                assert instrID[m] in cur_instr_obj.submodules, f"The submodule {instrID[m]} does not exist."
                cur_instr_obj = cur_instr_obj.submodules[instrID[m]]
            return cur_instr_obj
        else:
            assert instrID in self._station.components, f"Instrument by the name {instrID} has not been loaded. Call load_instrument on it first."
            return self._station.components[instrID]

    def print_HALs(self):
        print("Laboratory HALs:")
        for hal_ID in self._hal_objs:
            print(f"\t{hal_ID} (Type: {self.HAL(hal_ID).__class__.__name__})")
    def print_PROCs(self):
        print("Laboratory PROCs:")
        for proc_name in self._processors:
            print(f"\t{proc_name} (Type: {self.PROC(proc_name).__class__.__name__})")
    def print_WFMTs(self):
        print("Laboratory WFMTs:")
        for wfmt_name in self._waveform_transforms:
            print(f"\t{wfmt_name} (Type: {self.WFMT(wfmt_name).__class__.__name__})")
    def print_SPECs(self):
        print("Laboratory SPECs:")
        for spec_name in self._specifications:
            print(f"\t{spec_name}")


    def group_open(self, group_name):
        self._group_dir['Dir'] = group_name
        self._group_dir['InitDir'] = ""
        self._group_dir['SweepQueue'] = []
        self._group_dir['ExptIndex'] = -1

    def group_close(self):
        self._group_dir['Dir'] = ""
        self._group_dir['InitDir'] = ""
        self._group_dir['SweepQueue'] = []
        self._group_dir['ExptIndex'] = -1

    def _sweep_enqueue(self, var_name):
        self._group_dir['SweepQueue'].append(var_name)
    def _sweep_dequeue(self, var_name):
        self._group_dir['SweepQueue'].pop()

    def run_single(self, expt_obj, sweep_vars=[], **kwargs):
        #Get time-stamp
        if self._group_dir['Dir'] == "":
            folder_time_stamp = datetime.now().strftime(f"%Y-%m-%d/%H%M%S-" + expt_obj.Name + "/")
            self._group_dir['ExptIndex'] = -1
        else:
            if self._group_dir['InitDir'] == "":
                self._group_dir['InitDir'] = datetime.now().strftime(f"%Y-%m-%d/%H%M%S-{self._group_dir['Dir']}/")
            folder_time_stamp = self._group_dir['InitDir'] + datetime.now().strftime(f"%H%M%S-" + expt_obj.Name + "/")
        #Create the nested directory structure if it does not exist...
        cur_exp_path = self._save_dir + folder_time_stamp
        Path(cur_exp_path).mkdir(parents=True, exist_ok=True)

        #Reset kill-switch state
        self._kill_switch_reset(cur_exp_path)
        kwargs['kill_signal'] = self._kill_switch_check
        kwargs['kill_signal_send'] = self._kill_signal_send_internal

        #Verify and condition rec_params to be purely object-property pairs along with their unique resolution name...
        rec_params = kwargs.get('rec_params', [])
        assert isinstance(rec_params, list), "rec_params must be given as a list of parameters to track over the experiment."
        new_rec_params = []
        for m, cur_rec_param in enumerate(rec_params):
            if isinstance(cur_rec_param, tuple):
                assert len(cur_rec_param), "rec_param can only have doublets in the tuples - i.e. (sqdtoolz object, property name)."
                #Check that asserts in the object resolution don't trigger and that it resolves to a valid registered object
                obj_tree = self._resolve_sqdobj_tree(cur_rec_param[0])
                assert len(obj_tree) > 0, f"Object resolution failed for argument {m} in rec_param."
                #Check that the property exists in said object
                assert hasattr(cur_rec_param[0], cur_rec_param[1]), f"Property \'{cur_rec_param[1]}\' doesn't exist for object {m} in rec_param."
                #
                new_rec_param = [cur_rec_param[0], cur_rec_param[1]]
            else:
                #Check that asserts in the object resolution don't trigger and that it resolves to a valid registered object
                obj_tree = self._resolve_sqdobj_tree(cur_rec_param)
                assert len(obj_tree) > 0, f"Object resolution failed in the \'{cur_rec_param}\' for rec_param."
                #Check that Value exists in said object
                assert hasattr(cur_rec_param, 'Value'), f"Property \'Value\' doesn't exist for object {m} in rec_param. Perhaps specify a tuple to be sure?"
                #
                new_rec_param = [cur_rec_param, 'Value']
            cur_param_name = ".".join([f'{x[1]}_{x[0]}' for x in obj_tree])+f'.{new_rec_param[1]}'
            new_rec_params += [(new_rec_param[0], new_rec_param[1], cur_param_name)]
        kwargs['rec_params'] = new_rec_params

        #Setup marker for running Experiment (mostly to notify the ExperimentViewer...)
        exp_params = {'Configuration': expt_obj.ConfigName, 'SPECs': self.CONFIG(expt_obj.ConfigName).get_spec_names()}
        expt_param_file = self._save_dir + '_cur_exp.json'
        with open(expt_param_file, 'w') as outfile:
            json.dump(exp_params, outfile, indent=4, cls=customJSONencoder)

        ret_vals = expt_obj._run(cur_exp_path, sweep_vars, ping_iteration=self._update_progress_bar, **kwargs)
        self._group_dir['ExptIndex'] += 1

        #Save the experiment configuration
        self.save_experiment_configs(cur_exp_path)
        #Save experiment-specific experiment-configuration data (i.e. timing diagram)
        expt_obj.save_config(cur_exp_path, 'timing_diagram', 'experiment_parameters.txt', self._group_dir['SweepQueue'], self._group_dir['ExptIndex'])

        #Run postprocessing if the experiment completed
        if not self._killed_expt:
            expt_obj._post_process(ret_vals)
        
        #Save instrument configurations (QCoDeS)
        self._save_instrument_config(cur_exp_path)
        #Save Laboratory Configuration
        self.save_laboratory_config(cur_exp_path)
        
        #Save Laboratory Parameters
        self.save_variables(cur_exp_path)

        #Delete the currently running experiment parameters file...
        if os.path.exists(expt_param_file):
            os.remove(expt_param_file)

        self.update_state()
        return ret_vals

    def save_variables(self, cur_exp_path = '', file_name = 'laboratory_parameters.txt'):
        param_dict = {k:v._get_current_config() for (k,v) in self._variables.items()}
        with open(cur_exp_path + file_name, 'w') as outfile:
            # json.dump(param_dict, outfile)
            outfile.write(
                '{\n' +
                ',\n'.join(f"\"{x}\" : {json.dumps(param_dict[x], cls=customJSONencoder)}" for x in param_dict.keys()) +
                '\n}\n')

    def save_experiment_configs(self, cur_exp_path, file_name = 'experiment_configurations.txt'):
        dict_expt_configs = {x : self._expt_configs[x].get_config() for x in self._expt_configs}
        with open(cur_exp_path + file_name, 'w') as outfile:
            json.dump(dict_expt_configs, outfile, indent=4, cls=customJSONencoder)

    def save_laboratory_config(self, cur_exp_path, file_name = 'laboratory_configuration.txt'):
        #Prepare the dictionary of HAL configurations
        dict_hals = []
        for cur_hal in self._hal_objs:
            dict_hals.append(self._hal_objs[cur_hal]._get_current_config())

        #Prepare the dictionary of PROC configurations
        dict_procs = []
        for cur_proc in self._processors:
            dict_procs.append(self._processors[cur_proc]._get_current_config())

        #Prepare the dictionary of Waveform Transformations
        dict_wfmts = []
        for cur_wfmt in self._waveform_transforms:
            dict_wfmts.append(self._waveform_transforms[cur_wfmt]._get_current_config())

        #Prepare the dictionary of Experiment Specifications
        dict_specs = []
        for cur_spec in self._specifications:
            dict_specs.append(self._specifications[cur_spec]._get_current_config())

        param_dict = {
                    'ActiveInstruments' : self._activated_instruments,
                    'HALs' : dict_hals,
                    'PROCs': dict_procs,
                    'WFMTs': dict_wfmts,
                    'SPECs': dict_specs
                    }
        if cur_exp_path != '':
            with open(cur_exp_path + file_name, 'w') as outfile:
                json.dump(param_dict, outfile, indent=4, cls=customJSONencoder)
        return param_dict

    def _save_instrument_config(self, cur_exp_path):
        #Sometimes the configuration parameters use byte-values; those bytes need to be converted into strings
        #Code taken from: https://stackoverflow.com/questions/57014259/json-dumps-on-dictionary-with-bytes-for-keys
        def decode_dict(d):
            result = {}
            for key, value in d.items():
                if isinstance(key, bytes):
                    key = key.decode()
                if isinstance(value, bytes):
                    value = value.decode()
                elif isinstance(value, dict):
                    value = decode_dict(value)
                result.update({key: value})
            return result
        with open(cur_exp_path + 'instrument_configuration.txt', 'w') as outfile:
            raw_snapshot = self._station.snapshot_base()
            json.dump(decode_dict(raw_snapshot), outfile, indent=4, cls=customJSONencoder)


    @staticmethod
    def _printProgressBar(iteration, total, prefix = '', suffix = '', decimals = 1, length = 50, fill = '█', printEnd = "\r", prev_str='', using_vs_code=False):
        """
        Call in a loop to create terminal progress bar
        @params:
            iteration   - Required  : current iteration (Int)
            total       - Required  : total iterations (Int)
            prefix      - Optional  : prefix string (Str)
            suffix      - Optional  : suffix string (Str)
            decimals    - Optional  : positive number of decimals in percent complete (Int)
            length      - Optional  : character length of bar (Int)
            fill        - Optional  : bar fill character (Str)
            printEnd    - Optional  : end character (e.g. "\r", "\r\n") (Str)

        Adapted from: https://stackoverflow.com/questions/3173320/text-progress-bar-in-the-console
        """
        percent = ("{0:." + str(decimals) + "f}").format(100 * (iteration / float(total)))
        filledLength = int(length * iteration // total)
        bar = fill * filledLength + '-' * (length - filledLength)
        #The \033[K%s is to erase the entire line instead of leaving stuff behind when the string gets smaller... Except that doesn't work in Jupyter
        #https://github.com/jupyter/notebook/issues/4749 - so back to using the manual erasure...
        print(' '*len(prev_str), end = printEnd)
        if using_vs_code:
            magic_prefix = '\033[K\r'
        else:
            magic_prefix = '\033[K%s\r'
        ret_str = f'{magic_prefix}{prefix} |{bar}| {percent}% {suffix}'
        print(ret_str, end = printEnd)
        # Print New Line on Complete
        if iteration == total: 
            print()
        return ret_str

    def update_state(self):
        if self.UpdateStateEnabled:
            self.save_laboratory_config(self._save_dir, '_last_state.txt')
            self.save_variables(self._save_dir, '_last_vars.txt')
        self.save_experiment_configs(self._save_dir, '_last_exp_configs.txt')   #This is just a JSON transfer...
    def open_browser(self):
        cur_dir = os.path.dirname(os.path.realpath(__file__)).replace('\\','/')
        drive = cur_dir[0:2]
        full_abs_path = os.path.abspath(self._save_dir) + '/'    #Extra / as abspath doesn't include it...
        current_python_interpreter_path = sys.executable
        os.system(f'start \"temp\" cmd /k \"{drive} && cd \"{cur_dir}/Utilities\" && {current_python_interpreter_path} ExperimentViewer.py \"{full_abs_path}\"\"')

    def _kill_signal_send_internal(self):
        self._kill_internal = True
    def _kill_switch_reset(self, cur_exp_path):
        halt_loc = self._save_dir + 'HALT.txt'
        if os.path.exists(halt_loc):
            os.remove(halt_loc)
        self._kill_switch_dir = cur_exp_path
        self._killed_expt = False
        self._kill_internal = False
    def _kill_switch_check(self):
        halt_loc = self._save_dir + 'HALT.txt'
        if os.path.exists(halt_loc):
            os.remove(halt_loc)
            #Notify the experiment directory of the halting...
            open(self._kill_switch_dir + 'EXPERIMENT MANUALLY HALTED.txt', 'a').close()
            self._killed_expt = True
            return True
        elif self._kill_internal:
            self._kill_internal = False
            #Notify the experiment directory of the halting...
            open(self._kill_switch_dir + 'EXPERIMENT INTERNALLY HALTED.txt', 'a').close()
            self._killed_expt = True
            return True
        return False

    def _update_progress_bar(self, val_pct=0, reset=False):
        if reset:
            self._time_stamp_begin = time.time()
            self._time_stamps = [(0,0),]
            self._prog_bar_str = ''
            return

        self._time_stamps += [(val_pct, time.time())]

        ts = np.array([x[1] for x in self._time_stamps])
        pcts = np.array([x[0] for x in self._time_stamps])
        
        dTs = ts - self._time_stamp_begin
        dTs[0] = 0
        dTs = dTs[1:]-dTs[:-1]
        #
        dPcts = pcts[1:]-pcts[:-1]
        #
        dTbydPs = dTs/dPcts

        #Take an exponential distribution of the dT/dP (i.e. recent ones have a higher weighting) when taking the average: <dT/dP>
        self._time_stamps[-1][0]
        init_weight = 0.5
        
        if val_pct > 0:
            total_weight = np.sum(0.5*np.exp(-pcts[1:] * np.log(init_weight)/val_pct))
            average_weight = np.sum(dTbydPs * 0.5*np.exp(-pcts[1:] * np.log(init_weight)/val_pct) / total_weight)
            #
            time_left = average_weight*(1-val_pct)
            if time_left > 60:
                time_left = f"Est. time left: {(time_left/60.0):.2f}mins"
            else:
                time_left = f"Est. time left: {time_left:.2f}s"
        else:
            time_left = ""

        total_time = ts[-1] - self._time_stamp_begin
        if total_time > 60:
            total_time = f"Total time: {(total_time/60.0):.2f}mins"
        else:
            total_time = f"Total time: {total_time:.2f}s"
        
        if self._using_VS_Code:
            prog_bar_char = ""
        else:
            prog_bar_char = "\r"

        self._prog_bar_str = self._printProgressBar(int(val_pct*100), 100, suffix=f"{total_time}, {time_left}", prev_str=self._prog_bar_str, printEnd = prog_bar_char, using_vs_code=self._using_VS_Code)

        #Use the progress-bar ping as an opportunity to dump the current state of the instruments if update is enabled...
        self.update_state()
