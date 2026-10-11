# stepper4 Serial Interface Spec

Sep 28, 2026 · @Andrew

## Transport and framing

The link is USB serial at 115200 baud, 8N1, with no flow control. It carries plain ASCII lines.

- On a Raspberry Pi the port is `/dev/ttyACM0`, or `/dev/ttyUSB0` on CH340 clones.
- Host to device: one command per line, ending in `\n` (`\r\n` also works). A line can be up to 63 characters. Input is not case-sensitive.
- Device to host: every line ends in `\r\n`. Fields are separated by single spaces.
- Opening the port resets the Uno. Wait for `EVT BOOT 0 0 <version>`, about 2 s, before sending anything.
- The single byte `!` is handled as soon as it arrives, even in the middle of a line. It halts every motor instantly.

## Message grammar, tags, responses

Every command gets exactly one reply, `OK` or `ERR`, and the reply comes right away. A motion command's OK means it was **queued**. It does not mean it finished. Completion is reported later by an `EVT` line.

```
command  = ["#" tag SP] VERB {SP arg}
tag      = 1..65535          ; optional, echoed in the reply and in every event for that command
motor    = 1..4 | M1..M4     ; 0 = all motors, where the command allows it
reply    = "OK" [SP "#" tag] | "ERR" [SP "#" tag] SP code
```

- Tags are how the host links events to its commands. Use a fresh tag for every command.
- Data lines (`STA`, `INF`, `CFG`) come before the `OK` of the command that asked for them.
- `EVT` lines can arrive at any time. For STOP and HALT, the `CANCEL` and `DROP` events arrive before that command's own `OK`.
- Motors are fully independent. Sending a command to M2 never waits on M1, and the four queues run at the same time.

## Command reference

Motion commands go into that motor's queue, which holds 8 commands. Control commands take effect at once.

| Command | Args | Kind | Effect |
| --- | --- | --- | --- |
| `MOVE m steps [speed]` | steps ±, speed 1–10000 | queued | Relative move. + is forward, − is backward |
| `GOTO m pos [speed]` | absolute step position | queued | Move to a position |
| `JOG m dir [speed]` | dir `+`/`-` (or `F`/`R`) | queued | Run until stopped or a limit trips |
| `WAIT m ms` | 0–3,600,000 | queued | Pause that motor's queue for a set time |
| `STOP [m]` | m or 0/omitted = all | immediate | Decelerate, cancel the current command, flush the queue |
| `SKIP m` |  | immediate | Decelerate, cancel the current command, keep the queue |
| `HALT [m]` |  | immediate | Instant stop (no ramp), flush the queue |
| `CLEAR [m]` |  | immediate | Flush the queue and let the current command finish |
| `SPEED m sps` | 1–10000; m=0 for all | immediate | Default speed for commands that start later |
| `ACCEL m spss` | 1–200000 | immediate | Acceleration, steps/s² |
| `IDLE m ms` | 0–3,600,000; m=0 for all; 0 = hold | immediate | How long this motor must be idle before the drivers may power down (see below) |
| `ZERO m [pos]` |  | immediate | Set the position counter. The motor must be idle |
| `RELEASE m dir` | `+`/`-` | immediate | Back a motor off a switch that is already closed (for example at boot) |
| `STATUS [m]` |  | immediate | One `STA` line per motor |
| `INFO` |  | immediate | `INF` and `CFG` lines: firmware and limits |
| `EN 0\|1` |  | immediate | Disable or enable the drivers (0 also halts everything) |
| `ESTOP` |  | immediate | Latched stop with drivers off. Motion is refused until RESET |
| `RESET [m]` |  | immediate | Clear an E-stop latch or a motor FAULT |
| `WD ms` | 0 = off, up to 60000 | immediate | Host watchdog: halt all motors if the host goes silent |
| `PING` |  | immediate | Liveness check, and feeds the watchdog |
| `!` | single byte | real-time | Halt everything now |

`CFG` line (from INFO): `CFG m speed accel idle_ms`.

Driver power-down: all four drivers share one enable pin, so they power down only when **every** motor has been idle for at least its own `IDLE` time. A motor set to 0 keeps all of them powered. Power-down sends `EVT DRVOFF 0 0`. The motors then hold no position, so they can be pushed or back-driven. The next motion command powers the drivers back up.

`STA` line: `STA m state pos rate queued limit tag`. `state` is one of IDLE, RUN, DWELL, BACKOFF, FAULT, ESTOP. `rate` is signed steps/s, `limit` is 0 or 1, and `tag` belongs to the active command.

Example: start two motors and cancel one while the other keeps running.

```
> #1 MOVE 1 2000 3000
< OK #1
> #2 MOVE 2 -800
< OK #2
> #3 STOP 2
< EVT CANCEL 2 2
< OK #3
< EVT DONE 1 1
```

## Asynchronous events

Events have the form `EVT TYPE motor tag [extra]`. A motor of `0` means the event is system-wide. Each tagged command ends with exactly one of the terminal events: DONE, CANCEL, DROP, FAIL, FAULT or LIMIT.

| Event | Terminal | Extra | Meaning |
| --- | --- | --- | --- |
| `DONE` | yes |  | The command finished normally |
| `CANCEL` | yes |  | The command was stopped by STOP, SKIP, HALT or ESTOP |
| `DROP` | yes |  | The command was still queued and got discarded (STOP, HALT, limit trip, fault) |
| `FAIL` | yes | `LIMIT` | The command was refused because it moves toward a closed switch |
| `LIMIT` | yes | position | A limit tripped mid-move. Back-off starts automatically |
| `CLEAR` |  | position | Back-off finished. Carries the same tag as the LIMIT |
| `FAULT` | yes | `LIMIT_STUCK` | The switch never opened during back-off. Send RESET |
| `LIMSW` |  | (tag field = 0/1) | The raw limit switch changed state |
| `BOOT`, `ESTOP`, `HALT`, `WDOG`, `DRVOFF` |  |  | System events, sent with motor 0 and tag 0 |

## Error codes

| Code | Cause |
| --- | --- |
| `UNKNOWN` | The command verb isn't recognized |
| `SYNTAX` | Wrong number of arguments or a malformed line |
| `BADMOTOR` | The motor isn't 1–4 (or 0 where 0 is allowed) |
| `BADARG` | An argument is not a number or is out of range |
| `BADSPEED` | The speed is outside 1–10000 |
| `BADTAG` | The tag is outside 1–65535 |
| `QFULL` | That motor already has 8 queued commands |
| `BUSY` | ZERO or RELEASE was sent while the motor was moving |
| `NOTLIMIT` | RELEASE was sent but the switch isn't closed |
| `FAULT` | The motor is in FAULT. Send RESET first |
| `ESTOP` | An E-stop is latched (or RESET was sent while the E-stop input is still active) |
| `LINE_TOO_LONG` | The line is over 63 characters (sent with no tag) |

## Host guidance and Python example

Read the port on a dedicated thread and send commands from any thread. The reader matches each `OK`/`ERR` to its tag, and each terminal event to its tag. `stepper4_host.py` (pyserial) already does this.

```
from stepper4_host import Stepper4
s = Stepper4("/dev/ttyACM0", on_event=print, watchdog_ms=1000)
a = s.forward(1, 2000, speed=3000)   # returns tag immediately
b = s.backward(2, 800)               # M2 runs alongside M1
s.dwell(1, 500); c = s.goto(1, 0)    # M1's own sequence
s.stop(2)                            # cancel M2 only
s.wait(a); s.wait(c)                 # raises on LIMIT / CANCEL / FAULT
print(s.status())                    # {1: {'state': 'IDLE', 'pos': 0, ...}, ...}
```

- Keep no more than 8 commands outstanding per motor. The simplest way is to send the next one after each DONE.
- Turn on `WD` (for example 1000 ms) in production. The client then pings every third of that interval, so a crashed Pi app stops the motors.
- After `EVT BOOT` or any `LIMIT`/`FAULT`, treat positions as unverified until you re-home or `ZERO`.
- Use `!` (the client's `estop()`) for emergency stop from software. It skips the line parser entirely.
