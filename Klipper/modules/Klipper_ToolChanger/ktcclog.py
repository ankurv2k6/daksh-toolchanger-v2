# KTCC - Klipper Tool Changer Code
# Log and statistics module
#
# Copyright (C) 2023  Andrei Ignat <andrei@ignat.se>
#
# Based on and inspired by ERCF-Software-V3      Copyright (C) 2021  moggieuk#6538 (discord)
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#

# To try to keep terms apart:
# Mount: Tool is selected and loaded for use, be it a physical or a virtual on physical.
# Unmopunt: Tool is unselected and unloaded, be it a physical or a virtual on physical.
# Pickup: Tool is physically picked up and attached to the toolchanger head.
# Droppoff: Tool is physically parked and dropped of the toolchanger head.
# ToolLock: Toollock is engaged.
# ToolUnLock: Toollock is disengaged.

import logging, logging.handlers, threading, queue, time
import math, os.path, copy

# Forward all messages through a queue (polled by background thread)
class KtccQueueHandler(logging.Handler):
    def __init__(self, queue):
        logging.Handler.__init__(self)
        self.queue = queue

    def emit(self, record):
        try:
            self.format(record)
            record.msg = record.message
            record.args = None
            record.exc_info = None
            self.queue.put_nowait(record)
        except Exception:
            self.handleError(record)

# Poll log queue on background thread and log each message to logfile
class KtccQueueListener(logging.handlers.TimedRotatingFileHandler):
    def __init__(self, filename):
        logging.handlers.TimedRotatingFileHandler.__init__(
            self, filename, when='midnight', backupCount=5)
        self.bg_queue = queue.Queue()
        self.bg_thread = threading.Thread(target=self._bg_thread)
        self.bg_thread.start()

    def _bg_thread(self):
        while True:
            record = self.bg_queue.get(True)
            if record is None:
                break
            self.handle(record)

    def stop(self):
        self.bg_queue.put_nowait(None)
        self.bg_thread.join()

# Class to improve formatting of multi-line KTCC messages
class KtccMultiLineFormatter(logging.Formatter):
    def format(self, record):
        indent = ' ' * 9
        lines = super(KtccMultiLineFormatter, self).format(record)
        return lines.replace('\n', '\n' + indent)

class KtccLog:
    TOOL_UNKNOWN = -2
    TOOL_UNLOCKED = -1
    EMPTY_TOOL_STATS = {'toolmounts_completed': 0, 'toolunmounts_completed': 0, 'toolmounts_started': 0, 'toolunmounts_started': 0, 'time_selected': 0, 'time_heater_active': 0, 'time_heater_standby': 0, 'tracked_start_time_selected':0, 'tracked_start_time_active':0, 'tracked_start_time_standby':0, 'total_time_spent_unmounting':0, 'total_time_spent_mounting':0}
    KTCC_TOOL_STATISTICS_PREFIX = "ktcc_statistics_tool"

    def __init__(self, config):
        self.config = config
        self.gcode = config.get_printer().lookup_object('gcode')
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()

        self.printer.register_event_handler('klippy:connect', self.handle_connect)
        self.printer.register_event_handler("klippy:disconnect", self.handle_disconnect)
        self.printer.register_event_handler("klippy:ready", self.handle_ready)

        # Logging
        self.log_level = config.getint('log_level', 1, minval=0, maxval=3)
        self.logfile_level = config.getint('logfile_level', 3, minval=-1, maxval=4)
        self.log_statistics = config.getint('log_statistics', 0, minval=0, maxval=1)
        self.log_visual = config.getint('log_visual', 1, minval=0, maxval=2)

        # Logging
        self.queue_listener = None
        self.ktcc_logger = None

        # Save to file
        self.changes_to_save = False
        self.save_delay = 10
        self.save_active = True

        # Register commands
        handlers = [
            'KTCC_LOG_TRACE', 'KTCC_LOG_DEBUG', 'KTCC_LOG_INFO', 'KTCC_LOG_ALWAYS', 
            'KTCC_SET_LOG_LEVEL', 'KTCC_DUMP_STATS', 'KTCC_RESET_STATS',
            'KTCC_INIT_PRINT_STATS', 'KTCC_DUMP_PRINT_STATS']
        for cmd in handlers:
            func = getattr(self, 'cmd_' + cmd)
            desc = getattr(self, 'cmd_' + cmd + '_help', None)
            self.gcode.register_command(cmd, func, False, desc)

    def handle_ready(self):
        self.always('KlipperToolChangerCode Ready')

        # Wraping G28 and wait for temperature so we don't try sending gcode commands to save state while the gcode is blocked.
        # Need to do it outermost so that any G28 macros are used too.
        # When inside a G28 the parser won't run any SAVE_VARIABLE resulting in Klipper 
        try:
            self.toolhead = self.printer.lookup_object('toolhead')

            self.prev_G28 = self.gcode.register_command("G28", None)
            self.gcode.register_command("G28", self.cmd_KTCC_G28, desc = self.cmd_KTCC_G28_help)
        except Exception as e:
            logging.exception('KTCC Warning: Error trying to wrap G28 macro: %s' % str(e))

    cmd_KTCC_G28_help = "Homing axes."
    def cmd_KTCC_G28(self, gcmd):
        # self.trace("Starting G28")
        self.save_active = False                    # Don't try to use SAVE_VARIABLE commands.
        self.prev_G28(gcmd)
        self.save_active = True                     # Resume to use SAVE_VARIABLE commands.
        # self.trace("Ending G28")

    def _save_changes_timer_event(self, eventtime):
        try:
            if self.save_active and self.changes_to_save:
                self.changes_to_save = False
                self.trace("Saving state in logs.")

                self._persist_swap_statistics()
                self._persist_tool_statistics()
        except Exception as e:
            self.debug("_save_changes_timer_event:Exception: %s" % (str(e)))
            logging.exception("_save_changes_timer_event:Exception: %s" % (str(e)))
        nextwake = eventtime + self.save_delay
        return nextwake

    def handle_connect(self):
        # Load saved variables
        self.variables = self.printer.lookup_object('save_variables').allVariables

        # Setup background file based logging before logging any messages
        if self.logfile_level >= 0:
            logfile_path = self.printer.start_args['log_file']
            dirname = os.path.dirname(logfile_path)
            if dirname == None:
                ktcc_log = '/tmp/ktcc.log'
            else:
                ktcc_log = dirname + '/ktcc.log'
            self.debug("ktcc_log=%s" % ktcc_log)
            self.queue_listener = KtccQueueListener(ktcc_log)
            self.queue_listener.setFormatter(KtccMultiLineFormatter('%(asctime)s %(message)s', datefmt='%I:%M:%S'))
            queue_handler = KtccQueueHandler(self.queue_listener.bg_queue)
            self.ktcc_logger = logging.getLogger('ktcc')
            self.ktcc_logger.setLevel(logging.INFO)
            self.ktcc_logger.addHandler(queue_handler)

        # Load saved values
        self._load_persisted_state()

        # Init persihabele statistics
        self._reset_print_statistics()

        # Set up timer to save values when needed
        self.timer_save = self.reactor.register_timer(
            self._save_changes_timer_event, self.reactor.monotonic() + (self.save_delay))

    def handle_disconnect(self):
        self.always('KTCC Shutdown')
        self.reactor.update_timer(self.timer_save, self.reactor.NEVER)
        if self.queue_listener != None:
            self.queue_listener.stop()

    def _load_persisted_state(self):
        swap_stats = self.variables.get("ktcc_statistics_swaps", {})
        try:
            if swap_stats is None or swap_stats == {}:
                raise Exception("Couldn't find any saved statistics.")
            # self.trace("Loading statistics for KTCC: %s" % str(swap_stats))
            # self.total_mounts = swap_stats['total_mounts'] or 0
            self.total_time_spent_mounting = swap_stats['total_time_spent_mounting'] or 0
            self.total_time_spent_unmounting = swap_stats['total_time_spent_unmounting'] or 0
            self.total_toollocks = swap_stats['total_toollocks'] or 0
            self.total_toolunlocks = swap_stats['total_toolunlocks'] or 0
            self.total_toolmounts = swap_stats['total_toolmounts'] or 0
            self.total_toolunmounts = swap_stats['total_toolunmounts'] or 0
        except Exception:
            # Initializing statistics
            self._reset_statistics()

        self.tool_statistics = {}
        for tool in self.printer.lookup_objects('tool'):
            try:
                toolname=str(tool[0])
                toolname=toolname[toolname.rindex(' ')+1:]
                self.tool_statistics[toolname] = self.variables.get("%s%s" % (self.KTCC_TOOL_STATISTICS_PREFIX, toolname), self.EMPTY_TOOL_STATS.copy())
                self.tool_statistics[toolname]["tracked_start_time_selected"] = 0
                self.tool_statistics[toolname]["tracked_start_time_active"] = 0
                self.tool_statistics[toolname]["tracked_start_time_standby"] = 0
                self.tool_statistics[toolname]["tracked_unmount_start_time"] = 0
                self.tool_statistics[toolname]["tracked_mount_start_time"] = 0

            except Exception as err:
                self.debug("Unexpected error in toolstast: %s" % err)

    def _reset_print_statistics(self):
        # Init persihabele statistics
        self.print_time_spent_mounting = self.total_time_spent_mounting
        self.print_time_spent_unmounting = self.total_time_spent_unmounting
        self.print_toollocks = self.total_toollocks
        self.print_toolunlocks = self.total_toolunlocks
        self.print_toolmounts = self.total_toolmounts
        self.print_toolunmounts = self.total_toolunmounts
        self.print_tool_statistics = copy.deepcopy(self.tool_statistics)

####################################
# LOGGING FUNCTIONS                #
####################################
    def get_status(self, eventtime):
        return {'encoder_pos': "?"}

    #def always(self, message):
    #    if self.ktcc_logger:
    #        self.ktcc_logger.info(message)
    #    self.gcode.respond_info(message)

    def info(self, message):
        if self.ktcc_logger and self.logfile_level > 0:
            self.ktcc_logger.info(message)
        if self.log_level > 0:
            self.gcode.respond_info(message)

    def always(self, message):
            if self.ktcc_logger and self.logfile_level > 0:
                self.ktcc_logger.info(message)
            if self.log_level > 0:
                self.gcode.respond_info(message)

    def debug(self, message):
        message = "- DEBUG: %s" % message
        if self.ktcc_logger and self.logfile_level > 1:
            self.ktcc_logger.info(message)
        if self.log_level > 1:
            self.gcode.respond_info(message)

    def trace(self, message):
        message = "- - TRACE: %s" % message
        if self.ktcc_logger and self.logfile_level > 2:
            self.ktcc_logger.info(message)
        if self.log_level > 2:
            self.gcode.respond_info(message)

    # Fun visual display of KTCC state
    def _display_visual_state(self):
        if self.log_visual > 0 and not self.calibrating:
            self.always(self._state_to_human_string())

    def _log_level_to_human_string(self, level):
        log = "OFF"
        if level > 2: log = "TRACE"
        elif level > 1: log = "DEBUG"
        elif level > 0: log = "INFO"
        elif level > -1: log = "ESSENTIAL MESSAGES"
        return log

    def _visual_log_level_to_human_string(self, level):
        log = "OFF"
        if level > 1: log = "SHORT"
        elif level > 0: log = "LONG"
        return log



####################################
# STATISTICS FUNCTIONS             #
####################################
    def _reset_statistics(self):
        self.debug("Reseting KTCC statistics.")
        # self.total_mounts = 0
        self.total_time_spent_mounting = 0
        self.total_time_spent_unmounting = 0
        self.tracked_mount_start_time = 0
        # self.tracked_unmount_start_time = 0
        self.pause_start_time = 0
        self.total_toollocks = 0
        self.total_toolunlocks = 0
        self.total_toolmounts = 0
        self.total_toolunmounts = 0

        self.tool_statistics = {}
        for tool in self.printer.lookup_objects('tool'):
            try:
                toolname=str(tool[0])
                toolname=toolname[toolname.rindex(' ')+1:]
                self.tool_statistics[toolname] = self.EMPTY_TOOL_STATS.copy()
                self.tool_statistics[toolname]["tracked_start_time_selected"] = 0
                self.tool_statistics[toolname]["tracked_start_time_active"] = 0
                self.tool_statistics[toolname]["tracked_start_time_standby"] = 0
                self.tool_statistics[toolname]["tracked_unmount_start_time"] = 0
                self.tool_statistics[toolname]["tracked_mount_start_time"] = 0

            except Exception as err:
                self.debug("Unexpected error in toolstast: %s" % err)


    def track_mount_start(self, tool_id):
        self.trace("track_mount_start: Running for Tool: %s." % (tool_id))
        self._set_tool_statistics(tool_id, 'tracked_mount_start_time', time.time())
        

    def track_mount_end(self, tool_id):
        self.trace("track_mount_end: Running for Tool: %s." % (tool_id))
        start_time = self.tool_statistics[str(tool_id)]['tracked_mount_start_time']
        if start_time is not None and start_time != 0:
            # self.trace("track_mount_end: start_time is not None for Tool: %s." % (tool_id))
            time_spent = time.time() - start_time
            self.increase_tool_statistics(tool_id, 'total_time_spent_mounting', time_spent)
            self.total_time_spent_mounting += time_spent
            self._set_tool_statistics(tool_id, 'tracked_mount_start_time', 0)
            self.changes_to_save = True

    def track_unmount_start(self, tool_id):
        self.trace("track_unmount_start: Running for Tool: %s." % (tool_id))
        self._set_tool_statistics(tool_id, 'tracked_unmount_start_time', time.time())
        self.increase_tool_statistics(tool_id, 'toolunmounts_started')

    def track_unmount_end(self, tool_id):
        self.trace("track_unmount_end: Running for Tool: %s." % (tool_id))
        start_time = self.tool_statistics[str(tool_id)]['tracked_unmount_start_time']
        if start_time is not None and start_time != 0:
            # self.trace("track_unmount_end: start_time is not None for Tool: %s." % (tool_id))
            time_spent = time.time() - start_time
            self.increase_tool_statistics(tool_id, 'total_time_spent_unmounting', time_spent)
            self.total_time_spent_unmounting += time_spent
            self._set_tool_statistics(tool_id, 'tracked_unmount_start_time', 0)
            self.increase_tool_statistics(tool_id, 'toolunmounts_completed')
            self.increase_statistics('total_toolunmounts')
            self.changes_to_save = True


    def increase_statistics(self, key, count=1):
        try:
            self.trace("increase_statistics: Running. Provided to record tool stats while key: %s and count: %s" % (str(key), str(count)))
            if key == 'total_toolmounts':
                self.total_toolmounts += int(count)
            elif key == 'total_toolunmounts':
                self.total_toolunmounts += int(count)
            elif key == 'total_toollocks':
                self.total_toollocks += int(count)
            elif key == 'total_toolunlocks':
                self.total_toolunlocks += int(count)
            self.changes_to_save = True
        except Exception as e:
            self.debug("Exception whilst tracking tool stats: %s" % str(e))
            self.debug("increase_statistics: Error while increasing stats while key: %s and count: %s" % (str(key), str(count)))

    def track_selected_tool_start(self, tool_id):
        self.trace("track_selected_tool_start: Running for Tool: %s." % (tool_id))
        self._set_tool_statistics(tool_id, 'tracked_start_time_selected', time.time())
        self.increase_statistics('total_toolmounts')
        self.increase_tool_statistics(tool_id, 'toolmounts_completed')

    def track_selected_tool_end(self, tool_id):
        self.trace("track_selected_tool_end: Running for Tool: %s." % (tool_id))
        self._set_tool_statistics_time_diff(tool_id, 'time_selected', 'tracked_start_time_selected')
        self.changes_to_save = True

    def track_active_heater_start(self, tool_id):
        self.trace("track_active_heater_start: Running for Tool: %s." % (tool_id))
        self._set_tool_statistics(tool_id, 'tracked_start_time_active', time.time())

    def track_active_heater_end(self, tool_id):
        self.trace("track_active_heater_end: Running for Tool: %s." % (tool_id))
        self._set_tool_statistics_time_diff(tool_id, 'time_heater_active', 'tracked_start_time_active')
        self.changes_to_save = True

    def track_standby_heater_start(self, tool_id):
        self.trace("track_standby_heater_start: Running for Tool: %s." % (tool_id))
        self._set_tool_statistics(tool_id, 'tracked_start_time_standby', time.time())

    def track_standby_heater_end(self, tool_id):
        self.trace("track_standby_heater_end: Running for Tool: %s." % (tool_id))
        self._set_tool_statistics_time_diff(tool_id, 'time_heater_standby', 'tracked_start_time_standby')
        self.changes_to_save = True

    def _seconds_to_human_string(self, seconds):
        result = ""
        hours = int(math.floor(seconds / 3600.))
        if hours >= 1:
            result += "%d hours " % hours
        minutes = int(math.floor(seconds / 60.) % 60)
        if hours >= 1 or minutes >= 1:
            result += "%d minutes " % minutes
        result += "%d seconds" % int((math.floor(seconds) % 60))
        return result

    def _swap_statistics_to_human_string(self):
        msg = "KTCC Statistics:"
        # msg += "\n%d swaps completed" % self.total_mounts
        msg += "\n%s spent mounting tools" % self._seconds_to_human_string(self.total_time_spent_mounting)
        msg += "\n%s spent unmounting tools" % self._seconds_to_human_string(self.total_time_spent_unmounting)
        msg += "\n%d tool locks completed" % self.total_toollocks
        msg += "\n%d tool unlocks completed" % self.total_toolunlocks
        msg += "\n%d tool mounts completed" % self.total_toolmounts
        msg += "\n%d tool unmounts completed" % self.total_toolunmounts
        return msg

    def _swap_print_statistics_to_human_string(self):
        msg = "KTCC Statistics for this print:"
        # msg += "\n%d swaps completed" % self.total_mounts
        msg += "\n%s spent mounting tools" % self._seconds_to_human_string(self.total_time_spent_mounting-self.print_time_spent_mounting)
        msg += "\n%s spent unmounting tools" % self._seconds_to_human_string(self.total_time_spent_unmounting-self.print_time_spent_unmounting)
        msg += "\n%d tool locks completed" % (self.total_toollocks-self.print_toollocks)
        msg += "\n%d tool unlocks completed" % (self.total_toolunlocks-self.print_toolunlocks)
        msg += "\n%d tool mounts completed" % (self.total_toolmounts-self.print_toolmounts)
        msg += "\n%d tool unmounts completed" % (self.total_toolunmounts-self.print_toolunmounts)
        return msg

    def _division(self, dividend, divisor):
        try:
            return dividend/divisor
        except ZeroDivisionError:
            return 0

    def _dump_statistics(self, report=False):
        if self.log_statistics or report:
            msg = "ToolChanger Statistics:\n"
            msg += self._swap_statistics_to_human_string()
            msg += "\n------------\n"

            msg += "Tool Statistics:\n"

            # First convert to int so we get right order.
            res = {int(k):v for k,v in self.tool_statistics.items()}
            for tid in res:
                tool_id= str(tid)
                msg += "Tool#%s:\n" % (tool_id)
                msg += "Completed %d out of %d mounts in %s. Average of %s per toolmount.\n" % (self.tool_statistics[tool_id]['toolmounts_completed'], self.tool_statistics[tool_id]['toolmounts_started'], self._seconds_to_human_string(self.tool_statistics[tool_id]['total_time_spent_mounting']), self._seconds_to_human_string(self._division(self.tool_statistics[tool_id]['total_time_spent_mounting'], self.tool_statistics[tool_id]['toolmounts_completed'])))
                msg += "Completed %d out of %d unmounts in %s. Average of %s per toolunmount.\n" % (self.tool_statistics[tool_id]['toolunmounts_completed'], self.tool_statistics[tool_id]['toolunmounts_started'], self._seconds_to_human_string(self.tool_statistics[tool_id]['total_time_spent_unmounting']), self._seconds_to_human_string(self._division(self.tool_statistics[tool_id]['total_time_spent_unmounting'], self.tool_statistics[tool_id]['toolunmounts_completed'])))
                msg += "%s spent selected." % self._seconds_to_human_string(self.tool_statistics[tool_id]['time_selected'])
                tool = self.printer.lookup_object("tool " + str(tool_id))
                if tool.is_virtual != True or tool.name==tool.physical_parent_id:
                    if tool.extruder is not None:
                        msg += " %s with active heater and %s with standby heater." % (self._seconds_to_human_string(self.tool_statistics[tool_id]['time_heater_active']), self._seconds_to_human_string(self.tool_statistics[tool_id]['time_heater_standby']))
                msg += "\n------------\n"
                

        self.always(msg)

    def _dump_print_statistics(self, report=False):
        if self.log_statistics or report:
            msg = "ToolChanger Statistics for this print:\n"
            msg += self._swap_print_statistics_to_human_string()
            msg += "\n------------\n"

            msg += "Tool Statistics for this print:\n"

            # First convert to int so we get right order.
            res = {int(k):v for k,v in self.tool_statistics.items()}
            for tid in res:
                tool_id= str(tid)
                ts = self.tool_statistics[tool_id]
                pts = self.print_tool_statistics[tool_id]
                msg += "Tool#%s:\n" % (tool_id)
                msg += "Completed %d out of %d mounts in %s. Average of %s per toolmount.\n" % ((ts['toolmounts_completed']-pts['toolmounts_completed']), (ts['toolmounts_started']-pts['toolmounts_started']), self._seconds_to_human_string(ts['total_time_spent_mounting']-pts['total_time_spent_mounting']), self._seconds_to_human_string(self._division((ts['total_time_spent_mounting']-pts['total_time_spent_mounting']), (ts['toolmounts_completed']-ts['toolmounts_completed']))))
                msg += "Completed %d out of %d unmounts in %s. Average of %s per toolunmount.\n" % (ts['toolunmounts_completed']-pts['toolunmounts_completed'], ts['toolunmounts_started']-pts['toolunmounts_started'], self._seconds_to_human_string(ts['total_time_spent_unmounting']-pts['total_time_spent_unmounting']), self._seconds_to_human_string(self._division(ts['total_time_spent_unmounting']-pts['total_time_spent_unmounting'], ts['toolunmounts_completed']-pts['toolunmounts_completed'])))
                msg += "%s spent selected. %s with active heater and %s with standby heater.\n" % (self._seconds_to_human_string(ts['time_selected']-pts['time_selected']), self._seconds_to_human_string(ts['time_heater_active']-pts['time_heater_active']), self._seconds_to_human_string(ts['time_heater_standby']-pts['time_heater_standby']))
                msg += "------------\n"
        self.always(msg)



    def _persist_swap_statistics(self):
        swap_stats = {
            # 'total_mounts': self.total_mounts,
            'total_time_spent_mounting': round(self.total_time_spent_mounting, 1),
            'total_time_spent_unmounting': round(self.total_time_spent_unmounting, 1),
            'total_toolunlocks': self.total_toolunlocks,
            'total_toollocks': self.total_toollocks,
            'total_toolmounts': self.total_toolmounts,
            'total_toolunmounts': self.total_toolunmounts
            }
        self.toolhead.wait_moves()
        self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s VALUE=\"%s\"" % ("ktcc_statistics_swaps", swap_stats))

    def _persist_tool_statistics(self):
        for tool in self.tool_statistics:
            try:
                self.toolhead.wait_moves()
                self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s%s VALUE=\"%s\"" % (self.KTCC_TOOL_STATISTICS_PREFIX, tool, self.tool_statistics[tool]))
            except Exception as err:
                self.debug("Unexpected error in _persist_tool_statistics: %s" % err)

    def increase_tool_statistics(self, tool_id, key, count=1):
        try:
            self.trace("increase_tool_statistics: Running for Tool: %s. Provided to record tool stats while key: %s and count: %s" % (tool_id, str(key), str(count)))
            # if self.tool_statistics.get(str(tool_id)) is not None:
            if str(tool_id) in self.tool_statistics:
                if self.tool_statistics[str(tool_id)][key] is None:
                    self.tool_statistics[str(tool_id)][key]=0
                # self.trace("increase_tool_statistics: Before running for Tool: %s. Key: %s is: %s" % (tool_id, str(key), str(self.tool_statistics[str(tool_id)][key])))
                if isinstance(count, float):
                    self.tool_statistics[str(tool_id)][key] = round(self.tool_statistics[str(tool_id)][key] + count, 3)
                else:
                    self.tool_statistics[str(tool_id)][key] += count
                # self.trace("increase_tool_statistics: After running for Tool: %s. Key: %s is: %s" % (tool_id, str(key), str(self.tool_statistics[str(tool_id)][key])))
            else:
                self.debug("increase_tool_statistics: Unknown tool provided to record tool stats: %s" % tool_id)
                # self.debug(str(self.tool_statistics))
        except Exception as e:
            self.debug("Exception whilst tracking tool stats: %s" % str(e))
            self.debug("increase_tool_statistics: Error while tool: %s provided to record tool stats while key: %s and count: %s" % (tool_id, str(key), str(count)))
        # self.trace("increase_tool_statistics: Tool: %s provided to record tool stats while key: %s and count: %s" % (tool_id, str(key), str(count)))

    def _set_tool_statistics(self, tool_id, key, value):
        self.trace("_set_tool_statistics:Running for Tool: %s provided to record tool stats while key: %s and value: %s" % (tool_id, str(key), str(value)))
        try:
            if str(tool_id) in self.tool_statistics:
                self.tool_statistics[str(tool_id)][key] = value
            else:
                self.debug("_set_tool_statistics: Unknown tool: %s provided to record tool stats while key: %s and value: %s" % (tool_id, str(key), str(value)))
        except Exception as e:
            self.debug("Exception whilst tracking tool stats: %s" % str(e))
            self.debug("_set_tool_statistics: Error while tool: %s provided to record tool stats while key: %s and value: %s" % (tool_id, str(key), str(value)))
        # self.trace("_set_tool_statistics: Tool: %s provided to record tool stats while key: %s and value: %s" % (tool_id, str(key), str(value)))

    def _set_tool_statistics_time_diff(self, tool_id, final_time_key, start_time_key):
        try:
            if str(tool_id) in self.tool_statistics:
                tool_stat= self.tool_statistics[str(tool_id)]
                if tool_stat[start_time_key] is not None and tool_stat[start_time_key] != 0:
                    # self.trace("_set_tool_statistics_time_diff: Tool: %s value before running: final_time_key: %s=%s, start_time_key: %s=%s." % (tool_id, final_time_key, str(tool_stat[final_time_key]), start_time_key, str(tool_stat[start_time_key])))
                    if tool_stat[final_time_key] is not None and tool_stat[final_time_key] != 0:
                        tool_stat[final_time_key] += time.time() - tool_stat[start_time_key]
                    else:
                        tool_stat[final_time_key] = time.time() - tool_stat[start_time_key]
                    tool_stat[start_time_key] = 0
            else:
                self.debug("_set_tool_statistics_time_diff: Unknown tool: %s provided to record tool stats while final_time_key: %s and start_time_key: %s" % (tool_id, str(final_time_key), str(start_time_key)))
        except Exception as e:
            self.debug("Exception whilst tracking tool stats: %s" % str(e))
            self.debug("_set_tool_statistics_time_diff: Error while tool: %s provided to record tool stats while final_time_key: %s and start_time_key: %s" % (tool_id, str(final_time_key), str(start_time_key)))
        # self.trace("_set_tool_statistics_time_diff: Tool: %s value after running: final_time_key: %s=%s, start_time_key: %s=%s." % (tool_id, final_time_key, str(tool_stat[final_time_key]), start_time_key, str(tool_stat[start_time_key])))

### LOGGING AND STATISTICS FUNCTIONS GCODE FUNCTIONS

    cmd_KTCC_RESET_STATS_help = "Reset the KTCC statistics"
    def cmd_KTCC_RESET_STATS(self, gcmd):
        param = gcmd.get('SURE', "no")
        if param.lower() == "yes":
            self._reset_statistics()
            self._reset_print_statistics()
            self.changes_to_save = True
            self._dump_statistics(True)
            self.always("Statistics RESET.")
        else:
            message = "Are you sure you want to reset KTCC statistics?\n"
            message += "If so, run with parameter SURE=YES:\n"
            message += "KTCC_RESET_STATS SURE=YES"
            self.gcode.respond_info(message)

    cmd_KTCC_DUMP_STATS_help = "Dump the KTCC statistics"
    def cmd_KTCC_DUMP_STATS(self, gcmd):
        self._dump_statistics(True)

    cmd_KTCC_INIT_PRINT_STATS_help = "Run at start of a print to initialize the KTCC print statistics"
    def cmd_KTCC_INIT_PRINT_STATS(self, gcmd):
        self._reset_print_statistics()

    cmd_KTCC_DUMP_PRINT_STATS_help = "Run at end of a print to list statistics since last print reset."
    def cmd_KTCC_DUMP_PRINT_STATS(self, gcmd):
        self._dump_print_statistics(True)

    cmd_KTCC_SET_LOG_LEVEL_help = "Set the log level for the KTCC"
    def cmd_KTCC_SET_LOG_LEVEL(self, gcmd):
        self.log_level = gcmd.get_int('LEVEL', self.log_level, minval=0, maxval=4)
        self.logfile_level = gcmd.get_int('LOGFILE', self.logfile_level, minval=0, maxval=4)
        self.log_visual = gcmd.get_int('VISUAL', self.log_visual, minval=0, maxval=2)
        self.log_statistics = gcmd.get_int('STATISTICS', self.log_statistics, minval=0, maxval=1)

    cmd_KTCC_LOG_ALWAYS_help = "Log allways MSG"
    def cmd_KTCC_LOG_ALWAYS(self, gcmd):
        msg = gcmd.get('MSG')
        self.always(msg)

    cmd_KTCC_LOG_INFO_help = "Log info MSG"
    def cmd_KTCC_LOG_INFO(self, gcmd):
        msg = gcmd.get('MSG')
        self.info(msg)

    cmd_KTCC_LOG_DEBUG_help = "Log debug MSG"
    def cmd_KTCC_LOG_DEBUG(self, gcmd):
        msg = gcmd.get('MSG')
        self.debug(msg)

    cmd_KTCC_LOG_TRACE_help = "Log trace MSG"
    def cmd_KTCC_LOG_TRACE(self, gcmd):
        msg = gcmd.get('MSG')
        self.trace(msg)

    # def _get_print_status(self):
    #     try:
    #         # If using virtual sdcard this is the most reliable method
    #         source = "print_stats"
    #         print_status = self.printer.lookup_object("print_stats").get_status(self.printer.get_reactor().monotonic())['state']
    #     except:
    #         # Otherwise we fallback to idle_timeout
    #         source = "idle_timeout"
    #         if self.printer.lookup_object("pause_resume").is_paused:
    #             print_status = "paused"
    #         else:
    #             idle_timeout = self.printer.lookup_object("idle_timeout").get_status(self.printer.get_reactor().monotonic())
    #             if idle_timeout["printing_time"] < 1.0:
    #                 print_status = "standby"
    #             else:
    #                 print_status = idle_timeout['state'].lower()
    #     finally:
    #         self.trace("Determined print status as: %s from %s" % (print_status, source))
    #         return print_status


    # cmd_KTCC_STATUS_help = "Complete dump of current KTCC state and important configuration"
    # def cmd_KTCC_STATUS(self, gcmd):
    #     config = gcmd.get_int('SHOWCONFIG', 0, minval=0, maxval=1)
    #     msg = "KTCC with %d gates" % (len(self.selector_offsets))
    #     msg += " is %s" % ("DISABLED" if not self.is_enabled else "PAUSED/LOCKED" if self.is_paused else "OPERATIONAL")
    #     msg += " with the servo in a %s position" % ("UP" if self.servo_state == self.SERVO_UP_STATE else "DOWN" if self.servo_state == self.SERVO_DOWN_STATE else "unknown")
    #     msg += ", Encoder reads %.2fmm" % self._counter.get_distance()
    #     msg += "\nSelector is %shomed" % ("" if self.is_homed else "NOT ")
    #     msg += ". Tool %s is selected " % self._selected_tool_string()
    #     msg += " on gate %s" % self._selected_gate_string()
    #     msg += ". Toolhead position saved pending resume" if self.saved_toolhead_position else ""
    #     msg += "\nFilament position: %s" % self._state_to_human_string()
        
    #     if config:
    #         msg += "\n\nConfiguration:\nFilament homes"
    #         if self._must_home_to_extruder():
    #             if self.homing_method == self.EXTRUDER_COLLISION:
    #                 msg += " to EXTRUDER using COLLISION DETECTION (current %d%%)" % self.extruder_homing_current
    #             else:
    #                 msg += " to EXTRUDER using STALLGUARD"
    #             if self._has_toolhead_sensor():
    #                 msg += " and then"
    #         msg += " to TOOLHEAD SENSOR" if self._has_toolhead_sensor() else ""
    #         msg += " after a %.1fmm calibration reference length" % self._get_calibration_ref()
    #         if self.sync_load_length > 0 or self.sync_unload_length > 0:
    #             msg += "\nGear and Extruder steppers are synchronized during "
    #             load = False
    #             if self._has_toolhead_sensor() and self.sync_load_length > 0:
    #                 msg += "load (up to %.1fmm)" % (self.toolhead_homing_max)
    #                 load = True
    #             elif self.sync_load_length > 0:
    #                 msg += "load (%.1fmm)" % (self.sync_load_length)
    #                 load = True
    #             if self.sync_unload_length > 0:
    #                 msg += " and " if load else ""
    #                 msg += "unload (%.1fmm)" % (self.sync_unload_length)
    #         else:
    #             msg += "\nGear and Extruder steppers are not synchronized"
    #         msg += ". Tip forming current is %d%%" % self.extruder_form_tip_current
    #         msg += "\nSelector homing is %s - blocked gate detection and recovery %s possible" % (("sensorless", "may be") if self.sensorless_selector else ("microswitch", "is not"))
    #         msg += "\nClog detection is %s" % ("ENABLED" if self.enable_clog_detection else "DISABLED")
    #         msg += " and EndlessSpool is %s" % ("ENABLED" if self.enable_endless_spool else "DISABLED")
    #         p = self.persistence_level
    #         msg += ", %s state is persisted across restarts" % ("All" if p == 4 else "Gate status & TTG map & EndlessSpool groups" if p == 3 else "TTG map & EndlessSpool groups" if p == 2 else "EndlessSpool groups" if p == 1 else "No")
    #         msg += "\nLogging levels: Console %d(%s)" % (self.log_level, self._log_level_to_human_string(self.log_level))
    #         msg += ", Logfile %d(%s)" % (self.logfile_level, self._log_level_to_human_string(self.logfile_level))
    #         msg += ", Visual %d(%s)" % (self.log_visual, self._visual_log_level_to_human_string(self.log_visual))
    #         msg += ", Statistics %d(%s)" % (self.log_statistics, "ON" if self.log_statistics else "OFF")
    #     msg += "\n\nTool/gate mapping%s" % (" and EndlessSpool groups:" if self.enable_endless_spool else ":")
    #     msg += "\n%s" % self._tool_to_gate_map_to_human_string()
    #     msg += "\n\n%s" % self._swap_statistics_to_human_string()
    #     self._log_always(msg)

def load_config(config):
    return KtccLog(config)

