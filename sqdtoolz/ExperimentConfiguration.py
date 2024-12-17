from sqdtoolz.HAL.TriggerPulse import*
from sqdtoolz.Utilities.TimingPlots import*
from sqdtoolz.Variable import*
from sqdtoolz.HAL.WaveformGeneric import WaveformGeneric
import numpy as np
import json
import copy

class ExperimentConfiguration:
    def __init__(self, name, lab, duration, list_HALs, hal_ACQ = None, list_spec_names = [], **kwargs):
        self._name = name
        #Just register it to the labotarory - doesn't matter if it already exists as everything here needs to be reinitialised
        #to the new configuration anyway...
        lab._register_CONFIG(self)

        prev_config = kwargs.get('_hidden_config', None)
        if prev_config != None:
            self._total_time = prev_config._total_time
            self._lab = lab
            self._list_HALs = prev_config._list_HALs[:]
            self._hal_ACQ = prev_config._hal_ACQ
            self._list_spec_names = prev_config._list_spec_names[:]
            self._dict_wfm_map = copy.deepcopy(prev_config._dict_wfm_map)
            self._init_config = copy.deepcopy(prev_config._init_config)
            self.init_instruments()
        else:
            self._total_time = duration

            #Hard links are O.K. as the HAL objects won't get replaced on reinitialisation due to the design of __new__ in the Halbase
            #class. In addition, cold-restarts should have the strings properly instantiating the HALs once anyway. Nonetheless, the
            #update function will work with the laboratory class...
            self._lab = lab
            self._list_HALs = []
            for cur_hal in list_HALs:
                cur_hal_obj = lab.HAL(cur_hal)
                assert cur_hal_obj != None, f"Could not find HAL {cur_hal}."
                self._list_HALs += [cur_hal_obj]
            if hal_ACQ != None:
                cur_hal_obj = lab.HAL(hal_ACQ)
                assert cur_hal_obj != None, f"Could not find HAL {hal_ACQ}."
                self._hal_ACQ = cur_hal_obj
            else:
                self._hal_ACQ = None

            assert isinstance(list_spec_names, list), "Must give SPECs as a LIST of SPEC names."
            for cur_spec in list_spec_names:
                assert isinstance(cur_spec, str), "Each SPEC in the list must be given as a name (i.e. a STRING)."
            self._list_spec_names = list_spec_names[:]

            self._dict_wfm_map = {'waveforms' : {}, 'digital' : {} }

            self.save_config()

    def __new__(cls, *args, **kwargs):
        if len(args) == 0:
            name = kwargs.get('name', '')
        else:
            name = args[0]
        assert isinstance(name, str) and name != '', "Name parameter was not passed or does not exist as the first argument in the variable class initialisation?"
        if len(args) < 2:
            lab = kwargs.get('lab', None)
        else:
            lab = args[1]
        assert lab.__class__.__name__ == 'Laboratory' and lab != None, "Lab parameter was not passed or does not exist as the second argument in the variable class initialisation?"

        prev_exists = lab.CONFIG(name, True)
        if prev_exists:
            return prev_exists
        else:
            return super(cls.__class__, cls).__new__(cls)

    @classmethod
    def copyConfig(cls, name, lab, expt_config):
        assert isinstance(expt_config, ExperimentConfiguration), "Previous configuration must be a valid ExperimentConfiguration object."
        return cls(name, lab, -1, [], None, [], _hidden_config = expt_config)

    @property
    def Name(self):
        return self._name

    @property
    def RepetitionTime(self):
        return self._total_time
    @RepetitionTime.setter
    def RepetitionTime(self, len_seconds):
        self._total_time = len_seconds

    def __str__(self):
        cur_str = f"Name: {self.Name}\n"
        cur_str += f"RepetitionTime: {self.RepetitionTime}\n"
        cur_str += f"HALs: {[x.Name for x in self._list_HALs]}\n"
        if self._hal_ACQ == None:
            cur_str += f"ACQ: None\n"
        else:
            cur_str += f"ACQ: {self._hal_ACQ.Name}\n"
        cur_str += f"Processors: {[x.Name for x in self._proc_configs]}\n"
        cur_str += f"Experiment Specifications: {self._list_spec_names}\n"
        cur_str += f"WaveformMapping: {self._dict_wfm_map}"
        return cur_str

    def _settle_currently_used_processors(self, conf=None):
        self._proc_configs = []
        if conf != None:
            for cur_dict in conf['HALs']:
                if 'Processor' in cur_dict and cur_dict['Processor'] != '':
                    cur_proc = self._lab.PROC(cur_dict['Processor'])
                    if cur_proc != None:
                        self._proc_configs += [cur_proc]
                if 'ExProcessors' in cur_dict:
                    for proc_name in cur_dict['ExProcessors']:
                        cur_proc = self._lab.PROC(proc_name)
                        if cur_proc != None:
                            self._proc_configs += [cur_proc]
        else:            
            if self._hal_ACQ and hasattr(self._hal_ACQ, 'data_processor'):
                cur_proc = self._hal_ACQ.data_processor
                if cur_proc:
                    self._proc_configs = [cur_proc]
                if hasattr(self._hal_ACQ, '_post_processors'):  #TODO: Perhaps refactor to have ACQ return all used processors?
                    for pp_proc in self._hal_ACQ._post_processors:
                        self._proc_configs.append(pp_proc)

    def get_config(self):
        return self._init_config

    def save_config(self, file_name = ''):
        self._settle_currently_used_processors()
        
        cur_config = {'HALs' : [], 'PROCs' : [], 'RepetitionTime' : self.RepetitionTime, 'WaveformMapping' : self._dict_wfm_map, 'SPECs' : self._list_spec_names}

        #Prepare the dictionary of HAL configurations
        list_hals = self._list_HALs[:]
        if self._hal_ACQ is not None:
            list_hals += [self._hal_ACQ]

        for cur_hal in list_hals:
            if cur_hal != None:
                cur_config['HALs'].append(cur_hal._get_current_config())

        for cur_proc in self._proc_configs:
            cur_config['PROCs'].append(cur_proc._get_current_config())

        #Save to file if necessary
        if (file_name != ''):
            with open(file_name, 'w') as outfile:
                json.dump(cur_config, outfile, indent=4)
        
        self._init_config = cur_config
        return cur_config

    def update_config(self, conf, commit_changes_to_HALsPROCs=True, **kwargs):
        #TODO: Check if these checks here are probably overkill and possibly obsolete?
        for cur_dict in conf['HALs']:
            found_hal = False
            list_hals = self._list_HALs[:]
            if self._hal_ACQ is not None:
                list_hals += [self._hal_ACQ]
            for cur_hal in list_hals:
                if cur_hal == None:
                    continue
                if cur_hal.Name == cur_dict['Name']:
                    found_hal = True
                    if commit_changes_to_HALsPROCs:
                        for cur_key in kwargs:
                            cur_dict[cur_key] = kwargs[cur_key]
                        cur_hal._set_current_config(cur_dict, self._lab)
                    break
            assert found_hal, f"HAL object {cur_dict['Name']} does not exist in the current ExperimentConfiguration object."
        self._settle_currently_used_processors(conf)
        for cur_dict in conf['PROCs']:
            found_proc = False
            for cur_proc in self._proc_configs:
                if cur_proc.Name == cur_dict['Name']:
                    found_proc = True
                    if commit_changes_to_HALsPROCs:
                        cur_proc._set_current_config(cur_dict, self._lab)
                    break
            assert found_proc, f"PROC object {cur_dict['Name']} does not exist in the current ExperimentConfiguration object."
        self.RepetitionTime = conf['RepetitionTime']
        self._dict_wfm_map = conf['WaveformMapping']
        if 'SPECs' in conf:
           self._list_spec_names = conf['SPECs']
        #
        self._init_config = conf

    def map_waveforms(self, wfm_map_obj):
        self._dict_wfm_map = {
            'waveforms' : wfm_map_obj.waveforms,
            'digital'   : wfm_map_obj.digitals
        }
        #Convert marker objects into guids...
        if 'digital' in self._dict_wfm_map:
            for cur_dig in self._dict_wfm_map['digital']:
                if isinstance(self._dict_wfm_map['digital'][cur_dig], list):
                    self._dict_wfm_map['digital'][cur_dig] = [self._lab._resolve_sqdobj_tree(x) for x in self._dict_wfm_map['digital'][cur_dig]]                        
                else:
                    self._dict_wfm_map['digital'][cur_dig] = [self._lab._resolve_sqdobj_tree(self._dict_wfm_map['digital'][cur_dig])]
        self._init_config['WaveformMapping'] = self._dict_wfm_map

    def update_waveforms(self, wfm_gen, var_requests=[]):
        #var_requests is given as a list of tuples: (var_name, waveform_related_object, property_name)
        leVarRequests = []
        for cur_var_req in var_requests:
            assert isinstance(cur_var_req, (tuple, list)), "The argument var_requests must be a list of tuples: (var_name, waveform_related_object, property_name)"
            var_name, waveform_related_object, property_name = cur_var_req
            cur_parent = waveform_related_object.Parent
            resolution_tree = []
            cur_obj = waveform_related_object
            while (isinstance(cur_parent, (tuple, list)) and cur_parent[0] != None):
                resolution_tree += [( cur_obj.Name, cur_parent[1] )]
                cur_obj = cur_parent[0]
                final_wfm_name = cur_parent[1]
                cur_parent = cur_obj.Parent
            assert isinstance(cur_obj, WaveformGeneric), "The argument var_requests must only have things pertaining to the WaveformGeneric object wfm_gen..."
            resolution_tree = resolution_tree[::-1]
            #The first node will be (waveform_segment_name, waveform_name). This will be changed to (waveform_name, 'w')
            resolution_tree[0] = (resolution_tree[0][0], 'w')
            leVarRequests.append((var_name, final_wfm_name, resolution_tree, property_name))

        #Settle the actual waveforms first
        for cur_wave in  wfm_gen.waveforms:
            assert cur_wave in self._dict_wfm_map['waveforms'], f"There is no mapping for waveform \'{cur_wave}\'"
            awg_hal = None
            for cur_hal in self._list_HALs:
                if self._dict_wfm_map['waveforms'][cur_wave] == cur_hal.Name:
                    awg_hal = cur_hal
                    break
            assert awg_hal != None, f"The AWG waveform HAL {self._dict_wfm_map['waveforms'][cur_wave]} does not exist in this Experiment Configuration."
            awg_hal.set_waveform_segments(wfm_gen.waveforms[cur_wave])
            awg_hal.null_all_markers()
        #Now settle the output markers
        for cur_dig in wfm_gen.digitals:
            assert cur_dig in self._dict_wfm_map['digital'], f"There is no mapping for digital waveform \'{cur_dig}\'"
            cur_targets = self._dict_wfm_map['digital'][cur_dig]
            if 'refWaveform' in wfm_gen.digitals[cur_dig]:
                for cur_target in cur_targets:
                    cur_mkr = self._lab._get_resolved_obj(cur_target)

                    cur_mkr_awg = cur_target[0][0]
                    cur_ref_awg = self._dict_wfm_map['waveforms'][ wfm_gen.digitals[cur_dig]['refWaveform'] ]
                    if cur_mkr_awg == cur_ref_awg:
                        cur_mkr.set_markers_to_segments(wfm_gen.digitals[cur_dig]['segments'])
                    else:
                        mkr_wfm = self._lab.HAL(cur_ref_awg)._get_marker_waveform_from_segments(wfm_gen.digitals[cur_dig]['segments'])
                        assert mkr_wfm.size == self._lab.HAL(cur_mkr_awg).NumPts, "When setting a marker on a given waveform using reference segments from another waveforms, the two waveforms must be the same size."
                        cur_mkr.set_markers_to_arbitrary(mkr_wfm)
            else:
                for cur_target in cur_targets:
                    cur_trig = self._lab._get_resolved_obj(cur_target)
                    cur_trig.set_markers_to_trigger()
                    cur_trig.TrigPulseDelay = wfm_gen.digitals[cur_dig]['trig_delay']
                    cur_trig.TrigPulseLength = wfm_gen.digitals[cur_dig]['trig_length']
                    cur_trig.TrigPolarity = wfm_gen.digitals[cur_dig]['trig_polarity']
        #Now calculate and return any variable requests...
        ret_trans_vars = []
        for cur_var_req in leVarRequests:
            var_name, final_wfm_name, resolution_tree, property_name = cur_var_req
            assert final_wfm_name in self._dict_wfm_map['waveforms'], f"There is no mapping for waveform \'{final_wfm_name}\' from which to create a variable."
            for cur_hal in self._list_HALs:
                if self._dict_wfm_map['waveforms'][final_wfm_name] == cur_hal.Name:
                    awg_hal = cur_hal
                    break
            assert awg_hal != None, f"The AWG waveform HAL {self._dict_wfm_map['waveforms'][final_wfm_name]} does not exist in this Experiment Configuration."
            ret_trans_vars += [ VariablePropertyTransient(var_name, self._lab._get_resolved_obj([(awg_hal.Name, 'HAL')] + resolution_tree), property_name) ]
        return ret_trans_vars

    def init_instruments(self, **kwargs):
        cur_spec_targets = []
        #Get all parameters all ExperimentSpecifications will set
        for cur_spec in self._list_spec_names:
            spec_obj = self._lab.SPEC(cur_spec)
            if spec_obj != None:
                cur_spec_targets += spec_obj._get_targets()
        #Lock the properties that are to be set by the SPEC
        for cur_target in cur_spec_targets:
            cur_obj = self._lab._get_resolved_obj(cur_target[0])
            cur_obj._property_lock(cur_target[1])

        #Setup HALs and PROCs...
        self.update_config(self._init_config, **kwargs)

        #Unlock properties that are to be set by SPEC
        for cur_target in cur_spec_targets:
            cur_obj = self._lab._get_resolved_obj(cur_target[0])
            cur_obj._property_unlock(cur_target[1])

        #Setup SPECs...
        for cur_spec in self._list_spec_names:
            spec_obj = self._lab.SPEC(cur_spec)
            if spec_obj != None:
                spec_obj.commit_entries()
        

    def get_spec_names(self):
        return self._list_spec_names[:]

    def edit(self):
        self.init_instruments()
    def commit(self):
        self.save_config()

    def prepare_instruments(self):
        #TODO: Write rest of this with error checking

        list_hals = self._list_HALs[:]
        if self._hal_ACQ is not None:
            list_hals += [self._hal_ACQ]

        for cur_hal in list_hals:
            if cur_hal is not None and not cur_hal.ManualActivation:
                cur_hal.activate()
        
        for cur_hal in list_hals:
            #TODO: Write concurrence/change checks to better optimise AWG...
            cur_hal.prepare_initial()
        for cur_hal in list_hals:
            cur_hal.prepare_final()

    def makesafe_instruments(self):
        list_hals = self._list_HALs[:]
        if self._hal_ACQ is not None:
            list_hals += [self._hal_ACQ]
        for cur_hal in list_hals:
            if not cur_hal.ManualActivation:
                cur_hal.deactivate()

    def get_data(self):
        #TODO: Pack the data appropriately if using multiple ACQ objects (coordinating their starts/finishes perhaps?)
        cur_acq = self._hal_ACQ
        if cur_acq is not None:
            return cur_acq.get_data()
        else:
            return {'data': {'parameters':['None'],
                    'data':{'dummy_ch': np.array([0])}}}

    def get_trigger_edges(self, obj_trigger_input):
        assert isinstance(obj_trigger_input, TriggerInput), "The argument obj_trigger_input must be a TriggerInput object; that is, a genuine digital trigger input."

        cur_trig_srcs = []
        cur_input = obj_trigger_input
        cur_trig = obj_trigger_input._get_instr_trig_src()
        while(cur_trig != None):
            assert not (cur_trig in cur_trig_srcs),  "There is a cyclic dependency on the trigger sources. Look carefully at the HAL objects in play."
            cur_trig_srcs += [(cur_trig, cur_input._get_instr_input_trig_edge())]
            cur_input = cur_trig
            if isinstance(cur_trig._get_parent_HAL(), TriggerInputCompatible):
                cur_trig = cur_trig._get_instr_trig_src()
            else:
                break   #The tree had ended and this device is the root source...
            
        #Now spawn the tree of trigger times
        #TODO: Add error-checking to ensure the times do not overlap...
        all_times = np.array([])
        cur_gated_segments = np.array([])
        for cur_trig in cur_trig_srcs[::-1]:    #Start from the top of the tree...
            #Get the trigger times emitted by the current source cur_trig[0] given the input polarity cur_trig[1]
            cur_times, cur_gated_segments = cur_trig[0].get_trigger_times(cur_trig[1])
            cur_times = np.array(cur_times)
            if (len(all_times) == 0):
                all_times = cur_times
            else:
                #Translate the current trigger edges by the previous trigger edges
                cur_gated_segments = np.concatenate( [cur_gated_segments + prev_time for prev_time in all_times] )
                all_times = np.concatenate( [cur_times + prev_time for prev_time in all_times] )
        return (all_times, cur_gated_segments, cur_trig_srcs)

    def plot(self):
        '''
        Generate a representation of the timing configuration with matplotlib.
        
        Output:
            (Figure) matplotlib figure showing the timing configuration
        '''

        if self._total_time < 2e-6:
            scale_fac = 1e9
            plt_units = 'ns'
            min_tol = 0.2
        elif self._total_time < 2e-3:
            scale_fac = 1e6
            plt_units = 'us'
            min_tol = 0.001
        elif self._total_time < 2:
            scale_fac = 1e3
            plt_units = 'ms'
            min_tol = 1e-6
        else:
            scale_fac = 1
            plt_units = 's'
            min_tol = 1e-9

        tp = TimingPlot()

        disp_objs = []

        list_hals = self._list_HALs[:]
        if self._hal_ACQ is not None:
            list_hals += [self._hal_ACQ]

        for cur_hal in list_hals:
            if isinstance(cur_hal, TriggerInputCompatible):
                disp_objs += cur_hal._get_all_trigger_inputs()
            elif isinstance(cur_hal, TriggerOutputCompatible):
                disp_objs += [cur_hal._get_trigger_output_by_id(x) for x in cur_hal._get_all_trigger_outputs()]

        for cur_obj in disp_objs[::-1]:
            if isinstance(cur_obj, TriggerInput):
                trig_times, seg_times, cur_trig_srcs = self.get_trigger_edges(cur_obj)
                for cur_trig_src in cur_trig_srcs:
                    cur_HAL = cur_trig_src[0]._get_parent_HAL()
                    assert cur_HAL in self._list_HALs, f"HAL \"{cur_HAL.Name}\" is a trigger dependency in the tree for \"{cur_obj.Name}\". But \"{cur_HAL.Name}\" is not listed in this CONFIG {self.Name}."
            else:
                #TODO: Flag the root sources in diagram?
                trig_times = np.array([0.0])
                seg_times = np.array([])
            cur_diag_info = cur_obj._get_timing_diagram_info()
            if cur_diag_info['Type'] == 'None':
                continue
            
            tp.goto_new_row(cur_obj.Name)

            if cur_diag_info['Type'] == 'BlockShaded':
                if cur_diag_info['TriggerType'] == 'Gated':
                    if seg_times.size == 0:
                        continue
                    for cur_trig_seg in seg_times:
                        cur_trig_start = cur_trig_seg[0] * scale_fac
                        cur_len = (cur_trig_seg[1]-cur_trig_seg[0]) * scale_fac
                        tp.add_rectangle(cur_trig_start, cur_trig_start + cur_len)
                else:
                    for cur_trig_time in trig_times:
                        cur_trig_start = cur_trig_time * scale_fac
                        cur_len = cur_diag_info['Period'] * scale_fac
                        tp.add_rectangle(cur_trig_start, cur_trig_start + cur_len)
            elif cur_diag_info['Type'] == 'DigitalSampled':
                for cur_trig_time in trig_times:
                    cur_trig_start = cur_trig_time * scale_fac
                    mkrs, sample_rate = cur_diag_info['Data']
                    assert mkrs.size > 0, "Digital sampled pulse waveform is empty when plotting the timing diagram. Ensure _get_timing_diagram_info properly returns \'Type\' as \'None\' if inactive/empty..."
                    tp.add_digital_pulse_sampled(mkrs, cur_trig_start, scale_fac/sample_rate)
            elif cur_diag_info['Type'] == 'DigitalEdges':
                for cur_trig_time in trig_times:
                    cur_trig_start = cur_trig_time * scale_fac
                    pts = cur_diag_info['Data']
                    assert len(pts) > 0 and pts[0][0] == 0.0, "Digital pulse waveform must be non-empty and start with t=0.0 when plotting the timing diagram."
                    tp.add_digital_pulse(pts, cur_trig_start, scale_fac)
            elif cur_diag_info['Type'] == 'AnalogueSampled':
                for cur_trig_time in trig_times:
                    cur_trig_start = cur_trig_time * scale_fac
                    #Loop over all the waveform segments
                    cur_seg_x = cur_trig_start
                    for cur_wfm_dict in cur_diag_info['Data']:
                        cur_dur = cur_wfm_dict['Duration']*scale_fac
                        tp.add_rectangle_with_plot(cur_seg_x, cur_seg_x + cur_dur, cur_wfm_dict['yPoints'])
                        cur_seg_x += cur_dur
            else:
                assert False, "The \'Type\' key in the dictionary returned on calling the function _get_timing_diagram_info is invalid."

        return tp.finalise_plot(self._total_time*scale_fac, plt_units, f'Configuration: {self.Name}', tol=min_tol)

    def update_SPECs(self, list_spec_names):
        assert isinstance(list_spec_names, (list, tuple)), "Must give the SPECs as a list of names."
        self._list_spec_names = list_spec_names[:]
        self._init_config['SPECs'] = self._list_spec_names
