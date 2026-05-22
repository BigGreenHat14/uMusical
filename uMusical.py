from threading import Thread, Lock
from collections import deque
from argparse import ArgumentParser
import serial
import sys
from time import sleep
import midiutil


HELP = """
Command Refrence (# is id of speaker):
#<blanks> - Comment
d <blank> - Delay (seconds)
n # <blank> - Note (e.g. C4, in the case of MIDI output, the frequency argument is the duration plus 1000, otherwise its 1 second for compatability)
f # <blank> - Pitch in Hz (e.g. 440)
s # - Stop playing (In MIDI, the speaker id is the note to stop instead)
e <blanks> - Error message (e.g. Not enough speakers)
w <blanks> - Warning message (e.g. Only main melody, not enough speakers)
l <blanks> - Log message (e.g. Playing chorus)
m # # - Remap speaker (redirects commands, only 1 redirect, no chaining, applies to everything except this command)
v <blank> - Sets mode
o <blank> - Sets velocity (MIDI only)
c <blank> <blanks> - Conditional (see below)

Conditional command format:
c [m or c][<, >, =, ! or %][whole number] [command]
M is the mode parameter passed when running (use for whatever).
C is the count of speakers (use to make sure the main parts are always played).
If the statement is true (e.g. 3 speakers with the condition of c>2), the command is run, you can chain multiple for and statements.

REPL Comfort Commands:
get_mode - Self Ex.
get_speaker_count - Self Ex.
enable_debug - Same result as --debug flag
disable_debug - Same result as not using --debug flag (only needed if you enable debug in the middle of a file for some reason)
help - Displays this message again
"""


argparser = ArgumentParser(description="uMusical Conductor")
argparser.add_argument("--file", "-f", help="Path to the music file to play (*.umusic, use transpiler to convert midi)", required=False)
argparser.add_argument("--port", "-p", help="Serial port to use (of any MCU, for midi output, use midi#, where # is the midi port number, e.g. COM3)", required=True)
argparser.add_argument("--baudrate", "-r", help="Baudrate for serial communication (ignored for MIDI output)", default=115200, type=int)
argparser.add_argument("--mode", "-m", help="Mode value for music file", default=0, type=int)
argparser.add_argument("--delay", "-d", help="Delay between frequency changes in milliseconds (may make serial smoother but playback slower)", default=None, type=int) # Micropython is slow
argparser.add_argument("--debug", "-b", help="Enable debug output", action="store_true")
argparser.add_argument("--fast", help="Speaker count, enables you to run without requiring reset (must already me in receive mode, duck on dispaly)", default=None, type=int)

args = argparser.parse_args()

if args.delay is None:
    args.delay = 0 if args.port.startswith("midi") else 100 # mPy is slow, and MIDI doesn't need delays, so default to 0 for MIDI and 100 for MCUs

velocity = 64 # MIDI velocity, not used for MCU but whatever, might as well make it a variable for easy changing
speaker_remap = {}
debug = args.debug
mode = args.mode
speaker_count = None

serial_cache = deque()
serial_lock = Lock()

def _record_data():
    while serial_connection.is_open:
        try:
            line = serial_connection.readline()
        except: pass
        if line:
            with serial_lock:
                serial_cache.append(line.decode())

if args.port.startswith("midi"):
    import mido
    midi_port_number = int(args.port[4:])
    try:
        midi_output = mido.open_output(mido.get_output_names()[midi_port_number])
    except:
        print("MIDI port not found, exiting.")
        sys.exit()
    if debug: print(f"Opened MIDI output: {midi_output}")
else:
    try:
        port = args.port
    except IndexError:
        print("Device not found, exiting.")
        sys.exit()
    serial_connection = serial.Serial(
        port=port,
        baudrate=args.baudrate,
        timeout=1
    )
    record_thread = Thread(target=_record_data, daemon=True)
    record_thread.start()
    if debug: print(f"Opened serial connection on port: {port}")


def send_data(data):
    if debug: print(data)
    if isinstance(data, str):
        data = data.encode()
    serial_connection.write(data)

def send_line(data,readfix=True):
    send_data(data + "\r\n")
    if readfix:
        data = None
        while data == None:
            data = receive_line()

def receive_line():
    with serial_lock:
        if serial_cache:
            result = serial_cache.popleft()
            return result
        else:
            return None

def wait_for_line():
    while True:
        line = receive_line()
        if line is not None:
            return line

def note_to_midi(note_name: str) -> int:
    """
    Converts a note name (e.g., 'C4', 'A4', 'F#5', 'Bb3') to its frequency in Hz.
    Assumes standard A4 = 440Hz tuning.
    """
    # 1. Map note letters to their relative semitone position in an octave (starting at C)
    note_mapping = {
        'C': 0, 'C#': 1, 'DB': 1, 'D': 2, 'D#': 3, 'EB': 3, 
        'E': 4, 'F': 5, 'F#': 6, 'GB': 6, 'G': 7, 'G#': 8, 
        'AB': 8, 'A': 9, 'A#': 10, 'BB': 10, 'B': 11
    }
    
    # 2. Clean up input string
    note_name = note_name.strip().upper()
    
    # 3. Separate the note part from the octave number
    # Handles both 2-char (C4) and 3-char (F#4) note names
    if len(note_name) < 2:
        raise ValueError("Invalid note format. Expected format like 'C4' or 'F#5'.")
        
    if note_name[1] in ('#', 'B'):
        letter = note_name[:2]
        octave = int(note_name[2:])
    else:
        letter = note_name[0]
        octave = int(note_name[1:])
        
    if letter not in note_mapping:
        raise ValueError(f"Unknown note letter: {letter}")
        
    # 4. Calculate MIDI note number
    # MIDI note 0 is C-1, so C0 is 12, C4 is 60, A4 is 69, etc.
    return 12 * (octave + 1) + note_mapping[letter]

def note_to_frequency(note_name: str) -> float:
    midi_note = note_to_midi(note_name)
    
    # 5. Calculate frequency relative to A4 (MIDI 69) which is 440Hz
    frequency = 440.0 * (2.0 ** ((midi_note - 69) / 12.0))
    
    return round(frequency)

def wait_and_stop_midi(midi_output, note, delay):
    if delay <= 0:
        return
    sleep(delay)
    midi_output.send(mido.Message('note_off', note=note))

def run_command(command, trace_line=-1, depth=0):
    if not command[0].strip():
        return
    global debug, mode, velocity
    if debug: print(f"Executing command {idx + 1}: {command}","Line:", trace_line, "Depth:", depth)
    match command[0]:
        case "d":
            sleep(float(command[1]))
        case "n":
            if not args.port.startswith("midi"):
                run_command(["f", command[1], note_to_frequency(command[2])], trace_line=trace_line, depth=depth + 1)
            else:
                midi_output.send(mido.Message('note_on', note=note_to_midi(command[2]), velocity=velocity))
                Thread(target=wait_and_stop_midi, args=(midi_output, note_to_midi(command[2]), (1 if float(command[1]) < 1000 else float(command[1]) - 1000))).start() # Compatability (unless someone actually has 1000 buzzers and MCUs lying around, in which case they deserve it)
                sleep(args.delay / 1000)
        case "s":
            if not args.port.startswith("midi"):
                run_command(["f", command[1], 0], trace_line=trace_line, depth=depth + 1)
            else:
                try:
                    midi_output.send(mido.Message('note_off', note=note_to_midi(command[1])))
                except ValueError:
                    pass # Compatability with non-midi note off command, if the note doesn't exist just ignore it
        case "e":
            raise Exception(f"ERROR: {' '.join(command[1:])}")
        case "w":
            print(f"WARN: {' '.join(command[1:])}")
        case "l":
            print(f"LOG: {' '.join(command[1:])}")
        case "m":
            speaker_remap[int(command[1])] = int(command[2])
            if debug: print(f"Remapped speaker {command[1]} to {command[2]}")
        case "v":
            mode = int(command[1])
            if debug: print(f"Set mode to {mode}")
        case "f":
            try:
                if int(command[1]) > 2:
                    raise ValueError()
                int(command[2])
            except ValueError:
                raise SyntaxError(f"Invalid command (invalid argument for frequency command / frequency macro): {command}")
            if args.port.startswith("midi"): # MIDI!!!!
                raise NotImplementedError("Direct frequency control not supported for MIDI output, use note command instead.")
            else: # MCU :D
                send_line(f"{speaker_remap.get(int(command[1]), int(command[1]))} {command[2]}", readfix=False)
            sleep(args.delay / 1000)
        case "c":
            run_if = command[2:]
            condition = command[1]
            condition_id = condition[0] # m or c
            condition_op = condition[1] # <, >, =, ! (only int values)
            condition_value = int(condition[2:]) # whole number (no negatives)
            condition_resolved_id = speaker_count if condition_id == "c" else (mode if condition_id == "m" else (int(args.port.startswith("midi")) if condition_id == "i" else None))
            if debug: print(f"Condition: {condition_id} ({'speaker count' if condition_id == 'c' else ('mode' if condition_id == 'm' else ('is_midi' if condition_id == 'i' else 'unknown'))}) {condition_op} {condition_value} -> {condition_resolved_id} {condition_op} {condition_value}")
            if condition_resolved_id == None:
                raise SyntaxError(f"Invalid command (invalid condition ID in conditional command): {command}")
            
            match condition_op:
                case "<":
                    condition_result = condition_resolved_id < condition_value
                case ">":
                    condition_result = condition_resolved_id > condition_value
                case "=":
                    condition_result = condition_resolved_id == condition_value
                case "!":
                    condition_result = condition_resolved_id != condition_value
                case "%":
                    condition_result = condition_resolved_id % condition_value == 0
                case _:
                    raise SyntaxError(f"Invalid command (invalid operator in conditional command): {command}")
            
            if condition_result:
                run_command(run_if, trace_line=trace_line, depth=depth + 1)
        case "o":
            if not args.port.startswith("midi"):
                raise NotImplementedError("Only MIDI devices can change velocity.")
            velocity = int(command[1])
            if debug: print(f"Set velocity to {velocity}")
        # REPL comfort features (technichally could be used in files but not recommended since they make them non-deterministic and harder to debug):
        case "get_mode":
            print(mode)
        case "get_speaker_count":
            print(speaker_count)
        case "enable_debug":
            debug = True
            print("Debug enabled")
        case "disable_debug":
            if debug: print("Debug disabled")
            debug = False
        case "help":
            print(HELP)
        case "run_file":
            if len(command) < 2:
                raise SyntaxError(f"Invalid command (missing argument for run_file): {command}")
            with open(" ".join(command[1:]), "r") as f:
                file_commands = f.read().splitlines()
            for file_idx, line in enumerate(file_commands):
                if line.startswith("#") or not line.strip():
                    continue
                run_command(line.split(" "), trace_line=file_idx + 1, depth=depth + 1) # You can technically run files inside files, but please don't do that, it gets very confusing and is a pain to debug
        case _:
            raise SyntaxError(f"Invalid command (unknown command type): {command}")


if args.file is None:
    if not args.port.startswith("midi"):
        if args.fast is None:
            input("REPL: Reset Everything, then press enter > ")
            send_data(chr(3))
            speaker_count = int(wait_for_line().strip())
        else:
            speaker_count = args.fast
    else:
        speaker_count = 10000 # Midis too cool for speaker counts :D
    print("Loaded! Speaker count:", speaker_count, "Mode:", mode)
    idx = -1
    while True:
        try:
            user_input = input("> ")
            if not user_input.strip():
                continue
            run_command(user_input.split(" "), trace_line=-1, depth=0)
        except Exception as e:
            print(type(e).__name__,str(e))
with open(args.file, "r") as f:
    command_text = f.read().splitlines()

commands = []
for line in command_text:
    if line.startswith("#") or not line.strip():
        continue
    commands.append(line.split(" "))

if not args.port.startswith("midi"):
    if args.fast is None:
        print("Make sure to RESET EVERYTHING BEFORE playing!")
        input("(do that or everything will break) Press enter when ready > ")
        send_data(chr(3)) # Enter receive mode (also to get speaker count, and ctrl+c because single-threadedness is a pain)
        speaker_count = int(wait_for_line().strip())
    else:
        speaker_count = args.fast
else:
    speaker_count = 10000 # Midis too cool for speaker counts :D
print("Loaded! Speaker count:", speaker_count, "Mode:", mode)
input("Press enter when ready > ")
for idx, command in enumerate(commands):
    run_command(command, trace_line=idx + 1, depth=0)