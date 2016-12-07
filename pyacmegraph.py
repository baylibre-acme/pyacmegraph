#!/usr/bin/env python
""" ACME power probe capture and analysis tool

Connects to ACME devices, captures and displays data. Provides save/load
capabilities, as well as some computation features.
"""

import numpy as np
import pyqtgraph as pg
from pyqtgraph.Qt import QtGui, QtCore
from pyqtgraph.parametertree import Parameter, ParameterTree
import iio
import sys
import argparse
import struct
import threading
import time
import os
import copy
import pickle
import xmlrpc.client
import types
import re

__license__ = "MIT"
__status__ = "Development"

# ACME settings
integration_time = "0.000588"
in_oversampling_ratio = "1"
max_freq = 800  # experimental max sampling freq limit because if I2C link (in Hz)

parser = argparse.ArgumentParser(description='ACME measurements capture and display tool.',
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog='''
This tools captures exclusively Vbat and Vshunt values from ACME probes. Using Rshunt
(auto-detected or forced), it computes and displays the resulting power (Vbat*Vshunt/Rshunt).
Capture settings are automatically setup to optimize sampling resolution, but can be overriden.
Example usage:
''' + sys.argv[0] + ''' --ip baylibre-acme.local --shunts=100,50,250 -v
''')
parser.add_argument('--load', metavar='file',
                    help='''load .acme file containing data to display (switches
                        to display-only mode)''')
parser.add_argument('--template', metavar='file',
                    help='''load .acme file settings section only (colors,
                        plot names, shunts ...). Useful for starting a fresh
                        capture session re-using a previous saved setup''')
parser.add_argument('--inttime', metavar='value', nargs='?', default='',
                    help='integration time to use instead of default value ('
                    + integration_time + 's). Use without value to get the list '
                    'of accepted values')
parser.add_argument('--oversmplrt', metavar='value', type=int,
                    help='oversampling ratio to use instead of default value ('
                    + in_oversampling_ratio + ')')
parser.add_argument('--norelatime', action='store_true',
                    help='display absolute time from device')
parser.add_argument('--ip', help='IP address of ACME')
parser.add_argument('--shunts',
                    help='''list of shunts to use in mOhms (comma separated list,
                        one shunt value per channel, starting at channel 0) Ex: 100,50,250''')
parser.add_argument('--vbat', type=float, help=''' Force a constant Vbat value (in Volts)
                    to be used for computing power, in place of ACME measured vbat''')
parser.add_argument('--ishunt', action='store_true',
                    help='Display Ishunt instead of Power')
parser.add_argument('--forcevshuntscale', metavar='scale', nargs='?', default=0, type=float,
                    help='''Override Vshunt scale value, and force application start even
                    if identifying a Vshunt scaling problem''')
parser.add_argument('--verbose', '-v', action='count',
                    help='print debug traces (various levels v, vv, vvv)')

args = parser.parse_args()
if args.verbose >= 3:
    print "args: ", args

if args.oversmplrt and args.oversmplrt > 0:
    in_oversampling_ratio = str(args.oversmplrt)

# channels mapping: 'name used here' vs 'ACME naming'
cdict = {   'Vshunt' : 'voltage0',
            'Vbat' : 'voltage1',
            'Time' : 'timestamp',
            'Ishunt' : 'current3',
            'Power' : 'power2',
            }

# channels to enable (will be sent from ACME over I2C and up to app buffers)
enadict = { 'Vshunt' : True,
            'Vbat' : True,
            'Time' : True,
            }

# buffers content (column indexes)
plots_indexes = {   'time' : 0,
                    'pwr' : 1,
                    'vbat' : 2,
                    }

colors = [ "#0088FF", "#FF5500", "#449900", "#AA00AA", "#4444FF", "#994400", "#99AA00", "#990000" ]
vbat_colors = [ "#55CCFF", "#FFAA55", "#88DD00", "#FF00FF", "#9999FF", "#DD8833", "#DDFF00", "#FF3333" ]

# table containing all data for all channels
databufs = []
# dict containing all additional variables related to display settings
dispvars = {}

# state variable for initializing parameters from external file (template feature)
tmpl_setup = False

# Display power by default, but can display Ishunt alternatively (must be selected before init)
dispvars['display Ishunt'] = False

# default strings for displaying captured data (default Power, but can be changed to Ishunt)
dispstr = {}
dispstr['pwr_ishunt_str'] = "Power (mW)"
dispstr['pwr_plot_str'] = "Power plot"
dispstr['pwr_color_str'] = "Power color"

# Handle XMLRPC services related to an ACME device
class acmeXmlrpc():

    def __init__(self, address):
        serveraddr = "%s:%d" % (address, 8000)
        self.proxy = xmlrpc.client.ServerProxy("http://%s/acme" % serveraddr)
        print self.proxy

    # The info service provides informations not exposed through IIO
    def info(self, index):
        infod = { 'name': ''}
        try:
            info = self.proxy.info(index+1)
        except:
            if args.verbose >= 1:
                print "No XMLRPC service found for this device"
            return infod
        if str(info).find('Has Power Switch') != -1:
            infod['power switch'] = True
        match = re.match(r'PowerProbe (.+) \(', str(info))
        if match:
            infod['name'] = match.group(1)
        match = re.search(r'Serial Number: (\S+)', str(info))
        if match:
            infod['serial'] = match.group(1)

        return infod

# Handle a device (setup channels), retrieve and format data and store them into data buffer
# Then the main thread can read from data to plot it.
# The global data_thread_lock lock shall be used when accessing data.
class deviceThread(threading.Thread):

    def __init__(self, threadid, dev, rshunt, ndevices, enadict, vbat=0, ishunt=False, xmlrpc=None):

        threading.Thread.__init__(self)
        self.crdict = {}
        self.scaledict = {}
        self.abs_start_time = 0
        self.first_run = True
        self.running = True
        self.sample_period_stats_mean = 0
        self.estimated_freq = 0
        self.shunt_override = False
        self.buf = None
        self.power_switch = False
        self.meta = {}
        self.dev = dev
        self.ndevices = ndevices
        self.data = np.empty((0, 3))
        self.sample_period_stats = np.empty(0)
        self.enadict = enadict
        self.vbat = vbat
        self.ishunt = ishunt
        print "Configuring new device %d of %d. Name: %s ; id: %s" %(threadid + 1, ndevices, dev.name, dev.id)
        # set oversampling for max perfs (4 otherwise)
        dev.attrs['in_oversampling_ratio'].value = in_oversampling_ratio
        # enforce synchronous reads
        dev.attrs['in_allow_async_readout'].value = "0"
        if args.verbose >= 1:
            print "Showing attributes for %s" % (dev.id)
            for k, at in dev.attrs.items():
                print "   %s (%s)" % (at.name, at.value)
        print "---------------"
        # configuring channels for this device
        for k, v in cdict.items():
            ch = dev.find_channel(v)
            if ch:
                if args.verbose >= 1:
                    print "Found %s channel: %s (%s)" % (k, ch.id, ch.attrs['index'].value)
                if self.enadict.get(k):
                    if ch.attrs.get('scale'):
                        scale = float(ch.attrs.get('scale').value)
                        if k == "Time":
                            print "WARNING: scale on Time channel!!!"
                        # Check Vshunt scale
                        if k == "Vshunt" and scale != 0.0025:
                            print("Error: suspicious scale value on Vshunt channel" \
                                " (found %f instead of 0.0025 expected)!" % (scale))
                            print("Measurements may be wrong! Check ACME file-system version." \
                                    " (use --forcevshuntscale option to force app start)")
                            if args.forcevshuntscale == 0:
                                # argument not provided
                                sys.exit(0)
                        if k == "Vshunt" and args.forcevshuntscale != 0:
                            if args.forcevshuntscale == None:
                                print("Using default Vshunt scale value (%f)" % (scale))
                            else:
                                scale = args.forcevshuntscale
                                print("Forcing Vshunt scale to %f"% (scale))
                    else:
                        scale = 1.0
                    self.scaledict[k] = scale
                    if args.verbose >= 1:
                        print "   scale: %f" % (scale)
                    if ch.attrs.get("integration_time"):
                        # change integration time for max capture rate
                        ch.attrs.get("integration_time").value = integration_time
                    if args.verbose >= 1:
                        print "   enabling..."
                    ch.enabled = True
                # print ch.scan_element
                # print ch.attrs
                self.crdict[k] = ch
            else:
                print "Could not find %s channel..." % (k)
                sys.exit()
        self.sampling_freq_acme = float(dev.attrs['in_sampling_frequency'].value)
        # clip to the maximum sampling freq achieve-able with the BBB i2c bus
        # anyway, keep track of the acme setup for reference
        self.sampling_freq = int(min(max_freq, self.sampling_freq_acme) / self.ndevices)
        if args.verbose >= 1:
            print "Configured sampling frequency: %.0fHz (acme: %f)" % (self.sampling_freq, self.sampling_freq_acme)
        # Adjust buffer size based on expected frequency
        # size buffer to store 0.5s if possible
        buffer_size = int(self.sampling_freq / 2)
        if buffer_size < 64:
            buffer_size = 64
        if args.verbose >= 1:
            print "Adjusted buffer size to %d samples" % (buffer_size)
        self.buffer_size = buffer_size
        if rshunt == 0:
            # no override value passed, try to get it from device
            if dev.attrs.get("in_shunt_resistor"):
                rshunt = int(int(dev.attrs['in_shunt_resistor'].value) / 1000)
                if args.verbose >= 1:
                    print "Reading shunt value from device: %dmOhms" % rshunt
        else:
            self.shunt_override = True
        if rshunt == 0:
            # force a default value
            rshunt = 100
        self.rshunt = rshunt
        if args.verbose >= 1:
            print "Using shunt value: %dmOhms" % (self.rshunt)

        # Checking other device information through XML-RPC (if available)
        if type(xmlrpc) is not types.NoneType:
            self.meta = xmlrpc.info(threadid)
            if 'power switch' in self.meta:
                self.power_switch = True
            if args.verbose >= 1:
                print("Probe related meta data:")
                for key, elem in self.meta.items():
                    print("  %s: %s" %(key, elem))

        if args.verbose >= 1:
            print " ===================== "

    def run(self):
        self.buf = iio.Buffer(self.dev, self.buffer_size)
        if args.verbose >= 1:
            print "<%s> Starting %s" % (self.dev.id, self.dev.name)
            print "<%s> sample freq from device: %fHz" %(self.dev.id, float(self.dev.attrs['in_sampling_frequency'].value))
            print "<%s> Creating iio buffer, size = %d samples" % (self.dev.id, self.buffer_size)

        ti_last_start = 0.0
        while self.running:
            ti_start = time.time()

            self.buf.refill()
            ti_iiorefill = time.time()

            # Read and compute timer channel
            acmetime = self.crdict.get("Time").read(self.buf)
            unpack_str = 'q' * (len(acmetime) / struct.calcsize('q'))
            val_time = struct.unpack(unpack_str, acmetime)
            # do not apply scale on time
            # val_time = np.asarray(val_time) * scaledict.get("Time")
            val_time = np.asarray(val_time)
            # print "Read %d samples" % (len(val_time)) # reads a complete buffer each time
            if not args.norelatime:
                if self.first_run:
                    self.abs_start_time = val_time[0]
                val_time = val_time - self.abs_start_time

            # convert time from ns to ms (requires conversion from int to float - makes a table copy...)
            val_time = val_time.astype(float) / 1000000

            # Read channels and compute power on this bufer
            vshunt = self.crdict.get("Vshunt").read(self.buf)
            unpack_str = 'h' * (len(vshunt) / struct.calcsize('h'))
            val_vshunt = struct.unpack(unpack_str, vshunt)
            val_vshunt = np.asarray(val_vshunt) * self.scaledict.get("Vshunt")
            if self.enadict.get('Vbat') == True:
                vbat = self.crdict.get("Vbat").read(self.buf)
                val_vbat = struct.unpack(unpack_str, vbat)
                val_vbat = np.asarray(val_vbat) * self.scaledict.get("Vbat")
            else:
                # Use fixed value instead
                val_vbat = np.full(len(val_vshunt), int(self.vbat * 1000), dtype=int)

            if self.ishunt:
                # Compute Ishunt (in mA : 1000x mV / mO) instead of power
                val_power = (val_vshunt * 1000) / self.rshunt
            else:
                # compute power using minimal data (Vbat and Vshunt - we know Rshunt)
                # compute value in mW (mV x mV / mO)
                val_power = (val_vshunt * val_vbat) / self.rshunt

            if args.verbose >= 3:
                print "<%s>  Time (ns => ms) -------------------- " % (self.dev.id)
                print val_time
                print "<%s>  Vbat (mV) -------------------- " % (self.dev.id)
                print val_vbat
                print "<%s>  Vshunt (mV) -------------------- " % (self.dev.id)
                print val_vshunt
                if self.ishunt:
                    print "<%s>  Ishunt (mA) -------------------- " % (self.dev.id)
                else:
                    print "<%s>  Power (mW) -------------------- " % (self.dev.id)
                print val_power

            data_thread_lock.acquire()
            # Try to detect discontinuities
            if not self.first_run:
                # Compute buffer time since last buffer
                last_buf_time = val_time[0] - self.data[self.data.shape[0] - 1, 0]
                # trigger a warning if last time buffer is longer than 6 expected periods
                if last_buf_time > 6 * 1000/self.sampling_freq:
                    missed_samples = int((last_buf_time * self.sampling_freq) / 1000)
                    print "<%s> ** Warning: data overflow (and loss - %d samples) suspected!" % (self.dev.id, missed_samples)
                    print "<%s> ** last buf: %f, new buf: %f, diff(ms): %f, period (ms): %f" %(self.dev.id, self.data[self.data.shape[0] - 1, 0], val_time[0], last_buf_time, 1000/self.sampling_freq)
            ti_iioextract = time.time()

            # add new captured points to table
            tmp = self.data
            self.data = np.empty((self.data.shape[0] + self.buffer_size, 3))
            self.data[:tmp.shape[0]] = tmp
            self.data[tmp.shape[0]:, 0] = val_time
            self.data[tmp.shape[0]:, 1] = val_power
            self.data[tmp.shape[0]:, 2] = val_vbat
            data_thread_lock.release()
            ti_cpdata = time.time()

            estimated_freq = (1000 * self.buffer_size) / (val_time[val_time.shape[0] - 1 ] - val_time[0])
            if args.verbose >= 2:
                print "<%s>  iiorefill: %f; iioextract: %f; cpdata: %f; total: %f; (since last: %f) Freq: %.1fHz" % \
            (self.dev.id, ti_iiorefill - ti_start, ti_iioextract - ti_iiorefill, ti_cpdata - ti_iioextract, \
            ti_cpdata - ti_start, ti_start - ti_last_start, estimated_freq)
            if not self.first_run:
                # add last period element time in ms
                self.sample_period_stats = np.append(self.sample_period_stats, (ti_start - ti_last_start) * 1000)
                #only keep the last 10 period values
                self.sample_period_stats = self.sample_period_stats[-10:]
                # compute period mean
                self.sample_period_stats_mean = self.sample_period_stats.mean(0)
                # print self.sample_period_stats[-10:]
                # print "period: ", self.sample_period_stats_mean
                # print self.sample_period_stats
                self.estimated_freq = estimated_freq
            ti_last_start = ti_start

            if self.first_run:
                self.first_run = False


if args.vbat:
    print("Do not measure Vbat from ACME, and use a fixed Vbat value (%.3fV) to measure power" % (args.vbat))
    enadict['Vbat'] = False

def setup_ishunt():
    dispstr['pwr_ishunt_str'] = 'Ishunt (mA)'
    dispstr['pwr_plot_str'] = 'Ishunt plot'
    dispstr['pwr_color_str'] = 'Ishunt color'
    dispvars['display Ishunt'] = True

if args.ishunt:
    if not args.load and not args.template:
        setup_ishunt()
    else:
        print("Ignoring ishunt option (using settings from loaded acme file)")

if args.load:
    print "Reading %s file..." % (args.load)
    pkl_file = open(args.load, 'rb')
    dispvars = pickle.load(pkl_file)
    databufs = pickle.load(pkl_file)
    if args.verbose >= 2:
        print "Loaded data:"
        print databufs
    pkl_file.close()

if args.template:
    print "Reading %s file..." % (args.template)
    pkl_file = open(args.template, 'rb')
    dispvars = pickle.load(pkl_file)
    pkl_file.close()
    tmpl_setup = True

if dispvars['display Ishunt'] == True:
    # May have loaded Ishunt setup from file, so make sure to apply to it
    # to capture and / or menus
    args.ishunt = True
    setup_ishunt()

if not args.load:
    print "Connecting with ACME..."
    # IIO inits
    try:
        if args.ip:
            print "  Connecting with IP address: ", args.ip
            ctx = iio.Context("ip:" + args.ip)
            acme_address = args.ip
        else:
            print "  Connecting using iio fallback (IIOD_REMOTE=<%s>)" % (os.environ['IIOD_REMOTE'])
            ctx = iio.Context()
            acme_address = os.environ['IIOD_REMOTE']
    except:
        print "ERROR creating ACME iio context, aborting."
        sys.exit()

    if args.inttime == None:
        # option without arguments: fetch expected values, print them and exit
        print "  Please, use one of the following integration times:"
        print "    ", ctx.devices[0].attrs['integration_time_available'].value
        sys.exit()
    elif args.inttime:
        # try to use parameter passed
        if args.inttime in ctx.devices[0].attrs['integration_time_available'].value.split(' '):
            integration_time = args.inttime
            if args.verbose >= 1:
                print "Using passed integration time: ", integration_time
        else:
            print "Wrong integration time passed (%s), leaving..." % (args.inttime)
            print "Please, use one of the following integration times:"
            print "  ", ctx.devices[0].attrs['integration_time_available'].value
            sys.exit()

    # Get per channel shunt values, if provided
    # shunts table only used to pass override value (if any) at device init
    shunts = [ 0 ] * len(ctx.devices)   # 0 to not override shunt value
    if args.shunts:
        # get list of shunts from command-line and convert it to a list of int
        pshunt = map(int, args.shunts.split(','))
        # note that parameter list may be incomplete, so make sure shunts is padded with enough 0s
        shunts[0:len(pshunt)-1] = pshunt
    if args.verbose >= 2:
        print "  Using following shunts values, per device: ", shunts

    # Try to use XMLRPC service with ACME
    acme_xmlrpc = acmeXmlrpc(acme_address)

    # Create threads: 1 for each ACME detected device
    data_thread_lock = threading.Lock() # Lock used for any shared data buffer access
    threads = []
    thread_id = 0
    for d in ctx.devices:
        thread = deviceThread(thread_id, d, shunts[thread_id], len(ctx.devices),
                            enadict, args.vbat, args.ishunt, acme_xmlrpc)
        threads.append(thread)
        databufs.append({'gdata' : np.empty((0,3)), 'deviceid' : d.id, 'devicename' : d.name,
                        'name' : thread.meta['name']})
        thread_id += 1
    # print databufs
    # sys.exit()

    # Startup all threads after setup, so that sampling rates are consolidated
    if args.verbose >= 2:
        print threads
    for thread in threads:
        thread.start()



## Switch to using white background and black foreground
pg.setConfigOption('background', 'w')
pg.setConfigOption('foreground', 'k')

#generate layout
app = QtGui.QApplication([])
win = QtGui.QWidget()
l = QtGui.QGridLayout()
win.setLayout(l)

# Add configuration display
params_template = {'name': '', 'type': 'group', 'children': [
        {'name': 'Label', 'type': 'str', 'value': ""},
        {'name': '', 'type': 'bool', 'value': True, 'tip': "click to display this device " + dispstr['pwr_plot_str']},
        {'name': 'Color', 'type': 'color', 'value': "FF0", 'tip': "This is a color button"},
        {'name': '', 'type': 'bool', 'value': False, 'tip': "click to display this device Vbat plot"},
        {'name': 'Color', 'type': 'color', 'value': "FF0", 'tip': "This is a color button"},
    ]}
params = [{'name': 'Devices', 'type': 'group', 'children': []}]

# Add per channel parameters
for i, t in enumerate(databufs):
    params[0]['children'].append(copy.deepcopy(params_template))
    # group name: device
    params[0]['children'][i]['name'] = t['deviceid'] + " (" + t['devicename'] + ")"
    # label for convenience
    params[0]['children'][i]['children'][0]['name'] = 'label, ' + str(i)
    params[0]['children'][i]['children'][0]['value'] = t['name']
    # pwr plot enable
    params[0]['children'][i]['children'][1]['name'] = dispstr['pwr_plot_str'] + ', ' + str(i)
    # pwr plot color
    params[0]['children'][i]['children'][2]['name'] = dispstr['pwr_color_str'] + ', ' + str(i)
    params[0]['children'][i]['children'][2]['value'] = colors[i]
    # vbat plot enable
    params[0]['children'][i]['children'][3]['name'] = 'Vbat Plot, ' + str(i)
    # vbat plot color
    params[0]['children'][i]['children'][4]['name'] = 'Vbat color, ' + str(i)
    params[0]['children'][i]['children'][4]['value'] = vbat_colors[i]
    # print params
    # print "----"

if not args.load:
    # Add capture related settings (button for re-starting the capture, ...)
    devswitch_tmpl = {'name': '', 'type': 'bool', 'tip': 'Control ACME power switch for this device'}
    rshunt_tmpl = {'name': 'RshuntX', 'type': 'int', 'value': 10}
    smpl_period_tmpl = {'name': '', 'type': 'float', 'value': 0, 'readonly': True}
    est_freq_tmpl = {'name': '', 'type': 'int', 'value': 0, 'readonly': True}
    capturectrlb = {'name': 'Capture control', 'type': 'group', 'children': [
            {'name': 'Re-init buffers', 'type': 'action'},
            {'name': 'Power-switches', 'type': 'group', 'children': []},
            {'name': 'plot rate (ms)', 'type': 'int', 'value': 500},
            {'name': 'Rshunts (mOhms)', 'type': 'group', 'children': []},
            {'name': 'Buffer period stats (ms)', 'type': 'group', 'children': []},
            {'name': 'Samples per second', 'type': 'group', 'children': []},
            {'name': 'oversampling ratio', 'type': 'int', 'value': in_oversampling_ratio, 'readonly': True},
            {'name': 'integration time', 'type': 'str', 'value': integration_time, 'readonly': True},
            ]}
    freq_total = {'name': 'Total samples', 'type': 'int', 'value': 0, 'readonly': True}
    for i, t in enumerate(threads):
        if t.power_switch and 'in_active' in t.dev.attrs:
            ds = copy.deepcopy(devswitch_tmpl)
            ds['name'] = 'Device, ' + str(i)
            ds['value'] = t.dev.attrs['in_active'].value != '0'
            capturectrlb['children'][1]['children'].append(ds)
        rs = copy.deepcopy(rshunt_tmpl)
        rs['name'] = 'Rshunt, ' + str(i)
        rs['value'] = t.rshunt
        capturectrlb['children'][3]['children'].append(rs)
        per = copy.deepcopy(smpl_period_tmpl)
        per['name'] = "s:" + t.dev.id   # add heading 's' for 'sampling period'
        capturectrlb['children'][4]['children'].append(per)
        frq = copy.deepcopy(est_freq_tmpl)
        frq['name'] = "h:" + t.dev.id   # add heading 'h' for 'hertz'
        capturectrlb['children'][5]['children'].append(frq)
    capturectrlb['children'][5]['children'].append(freq_total)
    params.append(capturectrlb)

# Add distribution graph settings
thlistid = []
for t in databufs:
    thlistid.append(t['deviceid'] + ', pwr')
    thlistid.append(t['deviceid'] + ', vbat')
histalgos = [ 'auto', 'fd', 'doane', 'scott', 'rice', 'sturges', 'sqrt' ]
distrib = {'name': 'Distribution plot', 'type': 'group', 'children': [
        {'name': 'Dist enable', 'type': 'bool', 'value': False, 'tip': "click to enable distribution on zoom window"},
        {'name': 'dist. algo.', 'type': 'list', 'values': histalgos, 'value': 2},
        {'name': 'dev. select.', 'type': 'list', 'values': thlistid, 'value': 1},
    ]}
params.append(distrib)

# Add mouse pointer info
mousep_pwr_tmpl = {'name': 'Float', 'type': 'float', 'value': 0, 'step': 0.001, 'readonly': True}
mousep = {'name': 'Mouse pointer', 'type': 'group', 'children': [
        {'name': 'time', 'type': 'int', 'value': 0, 'readonly': True},
    ]}
for t in databufs:
    pwr = copy.deepcopy(mousep_pwr_tmpl)
    pwr['name'] = "p:" + t['deviceid']   # add heading 'p' for 'position'
    mousep['children'].append(pwr)
params.append(mousep)

# Add mean power values computation
zoomp_mean_tmpl = {'name': 'Float', 'type': 'float', 'value': 0, 'step': 0.001, 'readonly': True}
zoomp = {'name': 'Zoom plot', 'type': 'group', 'children': [
        {'name': 'width (ms)', 'type': 'float', 'value': 0, 'readonly': True},
        {'name': 'Mean ' + dispstr['pwr_ishunt_str'], 'type': 'group', 'children': []},
    ]}
for t in databufs:
    m = copy.deepcopy(zoomp_mean_tmpl)
    m['name'] = "m:" + t['deviceid']   # add heading 'm' for 'mean'
    zoomp['children'][1]['children'].append(m)

# Add Mean Vbat (mV) computation
vbat_mean_tmpl = {'name': 'Float', 'type': 'float', 'value': 0, 'step': 0.001, 'readonly': True}
vbatm = {'name': 'Mean Vbat (mV)', 'type': 'group', 'children': [
        {'name': 'Vbat enabled', 'type': 'bool', 'value': False, 'tip': "click to enable vbat mean computation on zoom window"},
    ]}
for t in databufs:
    m = copy.deepcopy(vbat_mean_tmpl)
    m['name'] = "v:" + t['deviceid']   # add heading 'v' for 'vbat'
    vbatm['children'].append(m)
zoomp['children'].append(vbatm)
params.append(zoomp)

# Add file operations menu
fileopm = {'name': 'File operations', 'type': 'group', 'children': [
            {'name': 'Save to binary (.acme)', 'type': 'action'},
            {'name': 'Save to text (.csv)', 'type': 'action'},
            {'name': 'Save to picture', 'type': 'action'},
            ]}
params.append(fileopm)


if args.verbose >= 2:
    print params


## Create tree of Parameter objects
try:
    pt = Parameter.create(name='params', type='group', children=params)
except:
    print "Error, Stopping threads..."
    print sys.exc_info()
    for t in threads:
        t.running = False
    for t in threads:
        t.join()
    sys.exit()

def reinit_buffers():
    data_thread_lock.acquire()
    for t in threads:
        t.data = np.empty((0, 3))
        t.first_run = True
    data_thread_lock.release()

def tree_trace_param(param, path, data):
    if args.verbose >= 2:
        print path
        if path is not None:
            childName = '.'.join(path)
        else:
            childName = param.name()
        print('  parameter: %s'% childName)
        print('  change:    %s'% change)
        print('  data:      %s'% str(data))

## If anything changes in the tree, print a message
def change(param, changes):
    # print("tree changes:")
    for param, change, data in changes:
        path = pt.childPath(param)
        # now update field if possible
        if not path:
            break
        if path[0] == 'Devices':
            tree_trace_param(param, path, data)
            if param.name().find('color') != -1:
                field, index = param.name().split(',')
                index = int(index)
                if field == dispstr['pwr_color_str']:
                    if type(data) is str:
                        col = data
                    elif type(data) is tuple:
                        # color update coming from saved preferences
                        col = '#' + format(data[0], '02x') + format(data[1], '02x') + format(data[2], '02x')
                        if args.verbose >= 1:
                            print "  Updating Pwer color (device %d) to <%s>" % (index, col)
                    else:
                        # we go a PyQt4.QtGui.QColor object
                        col = str(data.name())
                    if args.verbose >= 2:
                        print "setting " + dispstr['pwr_plot_str'] + " color of device %d to: %s" % (index, col)
                    colors[index] = col
                if field == 'Vbat color':
                    if type(data) is str:
                        col = data
                    elif type(data) is tuple:
                        # color update coming from saved preferences
                        col = '#' + format(data[0], '02x') + format(data[1], '02x') + format(data[2], '02x')
                        if args.verbose >= 1:
                            print "  Updating Vbat color (device %d) to <%s>" % (index, col)
                    else:
                        # we go a PyQt4.QtGui.QColor object
                        col = str(data.name())
                    if args.verbose >= 2:
                        print "setting vbat plot color of device %d to: %s" % (index, col)
                    vbat_colors[index] = col
            tree_trace_param(param, path, data)
            updateplots()
        elif path[0] == 'Distribution plot':
            display_histogram()
            tree_trace_param(param, path, data)
        elif path[0] == 'Zoom plot':
            if param.name() == 'Vbat enabled':
                updateplots()
                tree_trace_param(param, path, data)
        elif path[0] == 'Capture control':
            if len(path) > 1:
                if path[1] == 'Rshunts (mOhms)':
                    tree_trace_param(param, path, data)
                    if param.name().find('Rshunt,') != -1:
                        field, index = param.name().split(',')
                        index = int(index)
                        if field == 'Rshunt':
                            if tmpl_setup == False:
                                if args.verbose >= 2:
                                    print "Setting Rshunt of device %d to: %dmOhms" % (index, int(data))
                                threads[index].rshunt = int(data)
                            else:
                                if threads[index].shunt_override == False:
                                    if args.verbose >= 1:
                                        print "  Updating Rshunt of device %d to: %dmOhms" % (index, int(data))
                                    threads[index].rshunt = int(data)
                                else:
                                    # Load shunt from parameter only if not overriden from command line
                                    pt.child('Capture control', 'Rshunts (mOhms)', 'Rshunt, ' + str(index)).setValue(threads[index].rshunt)
                if path[1] == 'Power-switches':
                    tree_trace_param(param, path, data)
                    if param.name().find('Device') != -1:
                        field, index = param.name().split(',')
                        index = int(index)
                        threads[index].dev.attrs['in_active'].value = str(int(data))
            if param.name() == 'Re-init buffers':
                if args.verbose >= 2:
                    print "Re-init buffers!!!"
                reinit_buffers()
            elif param.name() == 'plot rate (ms)':
                if args.verbose >= 2:
                    print "changing timer interval to:", int(data)
                timer.setInterval(int(data))
                tree_trace_param(param, path, data)
        elif path[0] == 'File operations':
            if param.name() == 'Save to binary (.acme)':
                name = QtGui.QFileDialog.getSaveFileName(caption='Save captured data as binary file', filter="ACME captures .acme (*.acme)")
                if name:
                    # enforce .acme extension if no extension provided
                    filename, file_extension = os.path.splitext(str(name))
                    if not file_extension:
                        name += '.acme'
                    if savedatatofile(name):
                        if args.verbose >= 1:
                            print "Saved captured data to : ", name
            if param.name() == 'Save to text (.csv)':
                name = QtGui.QFileDialog.getSaveFileName(caption='Save captured data as text file', filter="ACME captures .csv (*.csv)")
                if name:
                    # create n files (1 per channel) and add .csv extension
                    filename, file_extension = os.path.splitext(str(name))
                    if filename:
                        for i, t in enumerate(databufs):
                            name = filename + "-ch" + str(i) + ".csv"
                            np.savetxt(name, t['gdata'], delimiter=",", header="Time (ms), " + dispstr['pwr_ishunt_str'] + ", Vbat (mV)")
                            if args.verbose >= 1:
                                print "Saving channel %d to %s file" % (i, name)
            elif param.name() == 'Save to picture':
                diag = QtGui.QFileDialog(caption="Capture plot picture to (.png) file")
                diag.setDefaultSuffix("png")
                diag.setNameFilter("Images .png (*.png)")
                if diag.exec_() == QtGui.QDialog.Accepted:
                    name = diag.selectedFiles()
                    time.sleep(1)   # wait for dialog widget to be closed, otherwise we capture it...
                    if QtGui.QPixmap.grabWindow(win.winId()).save(name[0], 'png'):
                        if args.verbose >= 1:
                            print "Saved image to: ", name[0]
            updateplots()

pt.sigTreeStateChanged.connect(change)

def savedatatofile(filename):
    output = open(filename, 'wb')
    print "trying to save..."
    dispvars['zoom range'] = region.getRegion()
    dispvars['ptree'] = pt.saveState()
    try:
        pickle.dump(dispvars, output, -1)
        data_thread_lock.acquire()
        pickle.dump(databufs, output, -1)
    except:
        data_thread_lock.release()
        return False


    data_thread_lock.release()
    print "save done..."

    output.close()
    return True


ptree = ParameterTree()
ptree.setParameters(pt, showTop=False)

# row , column, rowspan, colspan
l.addWidget(ptree, 1, 0, 4, 1)

label = pg.LabelItem(justify='right')

# Histogram
p0hist = pg.PlotWidget()
l.addWidget(p0hist, 1, 1)
p0hist.setMouseEnabled(x=True, y=False)
# p0hist.setVisible(False)

# zoom and real-time capture area
p1 = pg.PlotWidget()
l.addWidget(p1, 2, 1)
p1.setDownsampling(ds=True, auto=True, mode='peak')
p1.setClipToView(False)
p1.setMouseEnabled(x=True, y=False)
pi1 = p1.plotItem
p1ybis = pg.ViewBox()
pi1.showAxis('right')
pi1.scene().addItem(p1ybis)
pi1.getAxis('right').linkToView(p1ybis)
p1ybis.setXLink(pi1)

checkfreeze = QtGui.QCheckBox('Freeze display')
if args.load:
    checkfreeze.setChecked(True)
    checkfreeze.hide()
else:
    checkfreeze.setChecked(False)
l.addWidget(checkfreeze, 3, 1)

# global area
p2 = pg.PlotWidget()
l.addWidget(p2, 4, 1)
p2.setDownsampling(ds=True, auto=True, mode='peak')
p2.setClipToView(True)
p2.setMouseEnabled(x=True, y=False)
pi2 = p2.plotItem
p2ybis = pg.ViewBox()
pi2.showAxis('right')
pi2.scene().addItem(p2ybis)
pi2.getAxis('right').linkToView(p2ybis)
p2ybis.setXLink(pi2)

# Setup stretch factors so that increasing window size does not change tree size but increases plots
l.setColumnMinimumWidth(0, 300)
l.setColumnStretch(0, 0)
l.setColumnStretch(1, 1)
win.show()
win.resize(1280,768)

# Create selec-able region in intermediate zoom area
region = pg.LinearRegionItem()
region.setZValue(10)
# Add the LinearRegionItem to the ViewBox, but tell the ViewBox to exclude this
# item when doing auto-range calculations.

# Create selec-able region in global area
region2 = pg.LinearRegionItem()
region2.setZValue(10)

#pg.dbg()
# Configure zoom area display
p1.setAutoVisible(y=True)
p1.addLine(y=0)
p1.showGrid(x=True, y=True, alpha=0.30)

# Debug console with some context passed
# import pyqtgraph.console
# namespace = {'pg': pg, 'np': np, 'data1': data1, 'p1' : p1, 'region': region}
# c = pg.console.ConsoleWidget(namespace=namespace, text="debug console - sebj")
# c.show()
# c.setWindowTitle('pyqtgraph example: ConsoleWidget')

p1meanI = 0
p1meanP = 0
p1period = 0

# redraw p1 if user did modify region (only if display is frozen)
def update_region():
    if checkfreeze.isChecked():
        region.setZValue(10)
        minX, maxX = region.getRegion()
        p1.setXRange(minX, maxX, padding=0)
        p1period = maxX - minX
        display_histogram()
        update_zoomp()
        update_vbatm()

region.sigRegionChanged.connect(update_region)

# update region display in p2 if user changed zoom level in p1
def updateRegion(window, viewRange):
    if checkfreeze.isChecked():
        rgn = viewRange[0]
        region.setRegion(rgn)

p1.sigRangeChanged.connect(updateRegion)

def update_mouse_coords(mousepoint):
    time = mousepoint.x()
    pt.child('Mouse pointer', 'time').setValue(time)
    for t in databufs:
        gdata = t['gdata']
        index = gdata[:,0].searchsorted(time)
        # avoid out of bounds index when dragging region out of the window
        index = min(index, gdata.shape[0] - 1)
        if index > 0:
            power = gdata[index, 1]
        else:
            # empty table, can happen at init
            power = 0
        pt.child('Mouse pointer', "p:" + t['deviceid']).setValue(power)

def mouseMovedp0(evt):
    pt.child('Mouse pointer', 'time').setValue(0)
    if not pt.child('Distribution plot', 'Dist enable').value():
        for t in databufs:
            pt.child('Mouse pointer', "p:" + t['deviceid']).setValue(0)
        return
    pos = evt[0]  ## using signal proxy turns original arguments into a tuple
    mousePoint = p0hist.getPlotItem().getViewBox().mapSceneToView(pos)
    power = mousePoint.x()
    for t in databufs:
        if t['deviceid'] == pt.child('Distribution plot', 'dev. select.').value():
            pt.child('Mouse pointer', "p:" + t['deviceid']).setValue(power)
        else:
            pt.child('Mouse pointer', "p:" + t['deviceid']).setValue(0)

def mouseMovedp1(evt):
    pos = evt[0]  ## using signal proxy turns original arguments into a tuple
    mousePoint = p1.getPlotItem().getViewBox().mapSceneToView(pos)
    update_mouse_coords(mousePoint)

def mouseMovedp2(evt):
    pos = evt[0]  ## using signal proxy turns original arguments into a tuple
    mousePoint = p2.getPlotItem().getViewBox().mapSceneToView(pos)
    update_mouse_coords(mousePoint)

proxy0 = pg.SignalProxy(p0hist.scene().sigMouseMoved, rateLimit=60, slot=mouseMovedp0)
proxy1 = pg.SignalProxy(p1.scene().sigMouseMoved, rateLimit=60, slot=mouseMovedp1)
proxy2 = pg.SignalProxy(p2.scene().sigMouseMoved, rateLimit=60, slot=mouseMovedp2)

# Update zoom plot information area fields
def update_zoomp():
    # compute and display zoom plot width
    minX, maxX = region.getRegion()
    pt.child('Zoom plot', "width (ms)").setValue(maxX - minX)
    # compute each device mean power and display values
    for t in databufs:
        gdata = t['gdata']
        # print "range: %d, %d" % (gdata[:,0].searchsorted(minX), gdata[:,0].searchsorted(maxX))
        # print "gdata : ", gdata.shape
        mean = gdata[gdata[:,0].searchsorted(minX):gdata[:,0].searchsorted(maxX), 1].mean(0)
        pt.child('Zoom plot', 'Mean ' + dispstr['pwr_ishunt_str'], 'm:' + t['deviceid']).setValue(mean)

# Update vbat mean computation area fields
def update_vbatm():
    # only if feature is enabled
    if not pt.child('Zoom plot', 'Mean Vbat (mV)', 'Vbat enabled').value():
        for t in databufs:
            pt.child('Zoom plot', 'Mean Vbat (mV)', 'v:' + t['deviceid']).setValue(0)
        return
    # compute each device mean vbat and display values
    minX, maxX = region.getRegion()
    for t in databufs:
        gdata = t['gdata']
        mean = gdata[gdata[:,0].searchsorted(minX):gdata[:,0].searchsorted(maxX), 2].mean(0)
        pt.child('Zoom plot', 'Mean Vbat (mV)', 'v:' + t['deviceid']).setValue(mean)

def display_histogram():
    if not pt.child('Distribution plot', 'Dist enable').value():
        p0hist.clear()
        return

    # compute and display the distribution graph
    # device, variant = thlistid.index(pt.child('Distribution plot', 'dev. select.').value()).split(',')
    deviceid, variant = pt.child('Distribution plot', 'dev. select.').value().split(', ')
    for t in databufs:
        if t['deviceid'] == deviceid:
            gdata = t['gdata']
            break
    plot_index = plots_indexes[variant]
    # print "Updating distribution graph (device %d)" % (device)
    minX, maxX = region.getRegion()

    # compute histogram of this region
    y,x = np.histogram(gdata[gdata[:,0].searchsorted(minX):gdata[:,0].searchsorted(maxX), plot_index], bins=pt.child('Distribution plot', 'dist. algo.').value())
    # y,x = np.histogram(gdata[gdata[:,0].searchsorted(minX):gdata[:,0].searchsorted(maxX), 1], bins='fd')
    # y,x = np.histogram(gdata[gdata[:,0].searchsorted(minX):gdata[:,0].searchsorted(maxX), 1], bins=np.linspace(0, 10, 800))
    # y,x = np.histogram(data1[data1[:,0].searchsorted(minX):data1[:,0].searchsorted(maxX), 3], bins=np.linspace(0, 40, 400))
    # y,x = np.histogram(data1[data1[:,0].searchsorted(minX):data1[:,0].searchsorted(maxX), 3], bins='auto', range=(0,40))
    ## Using stepMode=True causes the plot to draw two lines for each sample.
    ## notice that len(x) == len(y)+1
    p0hist.plot(x, y, stepMode=True, fillLevel=0, brush=(0,128,128,150), clear=True)
    p0hist.setLabels(bottom=dispstr['pwr_ishunt_str'])
    # p1hist.plot(x[0:len(y)], y, clear=True)


## Handle view resizing
def updatep1Views():
    ## view has resized; update auxiliary views to match
    p1ybis.setGeometry(pi1.vb.sceneBoundingRect())

    ## need to re-update linked axes since this was called
    ## incorrectly while views had different shapes.
    ## (probably this should be handled in ViewBox.resizeEvent)
    p1ybis.linkedViewChanged(pi1.vb, p1ybis.XAxis)

pi1.vb.sigResized.connect(updatep1Views)

def updatep2Views():
    ## view has resized; update auxiliary views to match
    p2ybis.setGeometry(pi2.vb.sceneBoundingRect())

    ## need to re-update linked axes since this was called
    ## incorrectly while views had different shapes.
    ## (probably this should be handled in ViewBox.resizeEvent)
    p2ybis.linkedViewChanged(pi2.vb, p2ybis.XAxis)

pi2.vb.sigResized.connect(updatep2Views)

def updateplots(forcezoom=False):
    p1.clear()
    p1ybis.clear()
    p2.clear()
    p2ybis.clear()
    display_histogram()
    update_zoomp()
    update_vbatm()

    updatep1Views()
    updatep2Views()

    for i, t in enumerate(databufs):
        gdata = t['gdata']
        # p1, p2: plot all data and set visible range
        if pt.child('Devices', t['deviceid'] + " (" + t['devicename'] + ")", dispstr['pwr_plot_str'] + ', ' + str(i)).value():
            p1.plot(gdata[:,[0,1]], pen=colors[i])
            p2.plot(gdata[:,[0,1]], pen=colors[i])
        if pt.child('Devices', t['deviceid'] + " (" + t['devicename'] + ")", 'Vbat Plot, ' + str(i)).value():
            p1ybis.addItem(pg.PlotCurveItem(gdata[:,0], gdata[:,2], pen = vbat_colors[i]))
            p2ybis.addItem(pg.PlotCurveItem(gdata[:,0], gdata[:,2], pen = vbat_colors[i]))

    p0hist.enableAutoRange('y')
    p1.setLabels(left=dispstr['pwr_ishunt_str'], bottom='Time (ms)', right="Vbat (mV)")
    p1.enableAutoRange('y')
    p2.setLabels(left=dispstr['pwr_ishunt_str'], bottom='Time (ms)', right="Vbat (mV)")
    p2.addItem(region, ignoreBounds=True)
    p2.enableAutoRange('y')

    if forcezoom:
        # compute zoom window length
        gdata = databufs[0]['gdata']
        data_len = gdata[:,[0]].shape[0]
        if (data_len < 5 * threads[0].sampling_freq):
            zoom_len = data_len
        else:
            zoom_len = int(5 * threads[0].sampling_freq)
        # define visible range in p1: last zoom_len window
        minX, maxX = [ gdata[data_len - zoom_len, 0], gdata[data_len - 1, 0] ]
        p1.setXRange(minX, maxX, padding=0)
        region.setRegion((minX, maxX))
        p2.enableAutoRange('x')


def update_display():
    ti_start = time.time()
    if threads[0].first_run:
        # avoid boarder effects with plots if tables are empty
        return

    if not checkfreeze.isChecked():
        data_thread_lock.acquire()
        for i, t in enumerate(threads):
            # thread.gdata = np.copy(thread.data)
            # Make sure we make a deep copy of samples
            # we do not want to acces t.data outside of the lock
            databufs[i]['gdata'] = np.empty_like(t.data)
            databufs[i]['gdata'][:] = t.data
        data_thread_lock.release()
        ti_cp = time.time()

        updateplots(forcezoom=True)

        ti_plot = time.time()
        if args.verbose >= 2:
            print "     display update: cp: %.f, plot: %f  (total: %f)" %(ti_cp - ti_start, ti_plot - ti_cp, ti_plot - ti_start)
    total_freqs = 0
    for i, t in enumerate(databufs):
        pt.child('Capture control', 'Buffer period stats (ms)', 's:' + t['deviceid']).setValue(threads[i].sample_period_stats_mean)
        pt.child('Capture control', 'Samples per second', 'h:' + t['deviceid']).setValue(threads[i].estimated_freq)
        total_freqs += threads[i].estimated_freq
    pt.child('Capture control', 'Samples per second', 'Total samples').setValue(total_freqs)

if (args.load):
    pt.restoreState(dispvars['ptree'], addChildren=False, blockSignals=False)
    updateplots()
    region.setRegion(dispvars['zoom range'])
    win.setWindowTitle("ACME Power Audit - offline mode")
else:
    if args.verbose >= 1:
        print "Starting live capture mode..."
    win.setWindowTitle("ACME Power Audit - Live capture mode")
    timer = pg.QtCore.QTimer()
    timer.timeout.connect(update_display)
    timer.start(int(pt.child('Capture control', 'plot rate (ms)').value()))
    if args.template:
        if args.verbose >= 1:
            print "Setting parameters from saved .acme file..."
        pt.restoreState(dispvars['ptree'], addChildren=False, blockSignals=False)
        if args.verbose >= 1:
            print "Completed parameters setup"
        # Init passed, exit tmpl setup phase
        tmpl_setup = False
        # clear buffers to start clean (may have changed Rshunt)
        reinit_buffers()

## Start Qt event loop unless running in interactive mode or using pyside.
if __name__ == '__main__':
    import sys
    if (sys.flags.interactive != 1) or not hasattr(QtCore, 'PYQT_VERSION'):
        QtGui.QApplication.instance().exec_()

    if  not args.load:
        if args.verbose >= 2:
            print "Stopping threads..."
        for t in threads:
            t.running = False
        for t in threads:
            t.join()
