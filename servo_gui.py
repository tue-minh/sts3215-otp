"""
STS3215 Servo Motor GUI Controller
===================================
A simple tkinter GUI to control one or more STS3215 servo motors.

Features
--------
- Multi-motor selection (checkboxes, IDs 1-16)
- Selectable feedback parameters per motor
- Per-motor configurable limits (min/max angle, torque)
- Per-motor home position setting
- Press-and-hold motion button: motor moves while held, returns home on release
- Adjustable motion speed slider
- Torque ON/OFF controls
"""

from __future__ import annotations

import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Optional
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    from python_st3215 import ST3215
    from python_st3215.errors import ST3215Error
except ImportError as exc:
    _root = tk.Tk()
    _root.withdraw()
    messagebox.showerror(
        "Import Error",
        f"Cannot import python_st3215 library:\n{exc}\n\n"
        "Make sure the script is in the same folder as the python_st3215 package.",
    )
    sys.exit(1)


MAX_MOTOR_ID = 16
FEEDBACK_POLL_MS = 200
MOTION_CMD_INTERVAL = 0.05

FEEDBACK_PARAMS = [
    ("Position (steps)", "read_current_location"),
    ("Speed (steps/s)",  "read_current_speed"),
    ("Load (0.1%)",      "read_current_load"),
    ("Voltage (0.1V)",   "read_current_voltage"),
    ("Temperature (C)",  "read_current_temperature"),
    ("Current (6.5mA)",  "read_current_current"),
    ("Moving",           "read_mobile_sign"),
    ("Status",           "read_servo_status"),
]


class ServoGUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("STS3215 Servo Controller")
        self.root.resizable(True, True)

        self.controller: Optional[ST3215] = None
        self._feedback_job: Optional[str] = None
        self._motion_active = False
        self._motion_thread: Optional[threading.Thread] = None

        self._motor_selected: dict[int, tk.BooleanVar] = {}
        self._home_pos: dict[int, tk.IntVar] = {}
        self._min_angle: dict[int, tk.IntVar] = {}
        self._max_angle: dict[int, tk.IntVar] = {}
        self._torque_limit: dict[int, tk.IntVar] = {}

        self._fb_selected: dict[str, tk.BooleanVar] = {
            label: tk.BooleanVar(value=(i < 4))
            for i, (label, _) in enumerate(FEEDBACK_PARAMS)
        }

        self._build_ui()

    def _build_ui(self) -> None:
        pad = dict(padx=6, pady=4)

        # Connection
        conn_frame = ttk.LabelFrame(self.root, text="Connection")
        conn_frame.grid(row=0, column=0, columnspan=2, sticky="ew", **pad)

        ttk.Label(conn_frame, text="Port:").grid(row=0, column=0, sticky="w", **pad)
        self.port_var = tk.StringVar(value="COM3")
        ttk.Entry(conn_frame, textvariable=self.port_var, width=8).grid(row=0, column=1, **pad)

        ttk.Label(conn_frame, text="Baud:").grid(row=0, column=2, sticky="w", **pad)
        self.baud_var = tk.StringVar(value="1000000")
        ttk.Combobox(
            conn_frame, textvariable=self.baud_var,
            values=["1000000","500000","250000","128000","115200"],
            width=10, state="readonly"
        ).grid(row=0, column=3, **pad)

        self.connect_btn = ttk.Button(conn_frame, text="Connect", command=self._toggle_connection)
        self.connect_btn.grid(row=0, column=4, **pad)

        self.conn_status = ttk.Label(conn_frame, text="Disconnected", foreground="red")
        self.conn_status.grid(row=0, column=5, **pad)

        self.scan_btn = ttk.Button(conn_frame, text="Scan Motors", command=self._scan_motors, state="disabled")
        self.scan_btn.grid(row=0, column=6, **pad)

        # Left column
        left_frame = ttk.Frame(self.root)
        left_frame.grid(row=1, column=0, sticky="nsew", **pad)

        sel_frame = ttk.LabelFrame(left_frame, text="Motor Selection")
        sel_frame.pack(fill="x", **pad)
        ttk.Label(sel_frame, text="Select motors to control:").pack(anchor="w", padx=4)
        check_frame = ttk.Frame(sel_frame)
        check_frame.pack(fill="x", padx=4, pady=2)

        for mid in range(1, MAX_MOTOR_ID + 1):
            var = tk.BooleanVar(value=False)
            self._motor_selected[mid] = var
            col = (mid - 1) % 8
            row = (mid - 1) // 8
            ttk.Checkbutton(check_frame, text=str(mid), variable=var,
                            command=self._on_motor_selection_changed).grid(
                row=row, column=col, padx=3, pady=1, sticky="w"
            )

        param_frame = ttk.LabelFrame(left_frame, text="Per-Motor Parameters")
        param_frame.pack(fill="both", expand=True, **pad)

        headers = ["Motor ID", "Home Pos\n(steps)", "Min Angle\n(steps)",
                   "Max Angle\n(steps)", "Torque Limit\n(0-1000)"]
        for col, h in enumerate(headers):
            ttk.Label(param_frame, text=h, anchor="center", relief="groove", padding=3).grid(
                row=0, column=col, sticky="ew", padx=1
            )

        self._param_rows: dict[int, dict] = {}
        for mid in range(1, MAX_MOTOR_ID + 1):
            home_var = tk.IntVar(value=2048)
            min_var  = tk.IntVar(value=0)
            max_var  = tk.IntVar(value=4095)
            torq_var = tk.IntVar(value=1000)
            self._home_pos[mid]     = home_var
            self._min_angle[mid]    = min_var
            self._max_angle[mid]    = max_var
            self._torque_limit[mid] = torq_var

            lbl     = ttk.Label(param_frame, text=f"Motor {mid}", anchor="center")
            home_sb = ttk.Spinbox(param_frame, from_=-32766, to=32766, textvariable=home_var, width=7)
            min_sb  = ttk.Spinbox(param_frame, from_=0, to=4094, textvariable=min_var,  width=7)
            max_sb  = ttk.Spinbox(param_frame, from_=1, to=4095, textvariable=max_var,  width=7)
            torq_sb = ttk.Spinbox(param_frame, from_=0, to=1000, textvariable=torq_var, width=7)

            self._param_rows[mid] = dict(lbl=lbl, home=home_sb, min=min_sb, max=max_sb, torq=torq_sb)
            for col, widget in enumerate([lbl, home_sb, min_sb, max_sb, torq_sb]):
                widget.grid(row=mid, column=col, padx=2, pady=1, sticky="ew")
                widget.grid_remove()

        # Right column
        right_frame = ttk.Frame(self.root)
        right_frame.grid(row=1, column=1, sticky="nsew", **pad)
        self.root.columnconfigure(1, weight=1)
        self.root.rowconfigure(1, weight=1)

        fb_sel_frame = ttk.LabelFrame(right_frame, text="Feedback Parameters to Display")
        fb_sel_frame.pack(fill="x", **pad)
        for i, (label, _) in enumerate(FEEDBACK_PARAMS):
            ttk.Checkbutton(fb_sel_frame, text=label, variable=self._fb_selected[label],
                            command=self._rebuild_feedback_table).grid(
                row=i // 4, column=i % 4, sticky="w", padx=6, pady=2
            )

        fb_frame = ttk.LabelFrame(right_frame, text="Live Feedback")
        fb_frame.pack(fill="both", expand=True, **pad)
        fb_canvas = tk.Canvas(fb_frame, height=200)
        fb_scrollbar = ttk.Scrollbar(fb_frame, orient="vertical", command=fb_canvas.yview)
        self._fb_inner = ttk.Frame(fb_canvas)
        self._fb_inner.bind("<Configure>",
            lambda e: fb_canvas.configure(scrollregion=fb_canvas.bbox("all")))
        fb_canvas.create_window((0, 0), window=self._fb_inner, anchor="nw")
        fb_canvas.configure(yscrollcommand=fb_scrollbar.set)
        fb_canvas.pack(side="left", fill="both", expand=True)
        fb_scrollbar.pack(side="right", fill="y")
        self._fb_labels: dict[int, dict[str, ttk.Label]] = {}

        motion_frame = ttk.LabelFrame(right_frame, text="Motion Control")
        motion_frame.pack(fill="x", **pad)

        ttk.Label(motion_frame, text="Speed (steps/s):").grid(row=0, column=0, sticky="w", **pad)
        self.speed_var = tk.IntVar(value=500)
        ttk.Scale(motion_frame, from_=0, to=5000, orient="horizontal",
                  variable=self.speed_var, length=220).grid(row=0, column=1, **pad)
        self.speed_label = ttk.Label(motion_frame, text="500", width=6)
        self.speed_label.grid(row=0, column=2, **pad)
        self.speed_var.trace_add("write", lambda *_: self.speed_label.configure(text=str(self.speed_var.get())))

        ttk.Label(motion_frame, text="Direction:").grid(row=1, column=0, sticky="w", **pad)
        self.direction_var = tk.StringVar(value="CW")
        ttk.Radiobutton(motion_frame, text="CW (+)", variable=self.direction_var, value="CW").grid(row=1, column=1, sticky="w", **pad)
        ttk.Radiobutton(motion_frame, text="CCW (-)", variable=self.direction_var, value="CCW").grid(row=1, column=2, sticky="w", **pad)

        ttk.Label(motion_frame, text="Move offset (steps):").grid(row=2, column=0, sticky="w", **pad)
        self.offset_var = tk.IntVar(value=500)
        ttk.Spinbox(motion_frame, from_=0, to=32766, textvariable=self.offset_var, width=8).grid(row=2, column=1, sticky="w", **pad)

        self.motion_btn = tk.Button(
            motion_frame, text="HOLD to Move",
            font=("TkDefaultFont", 11, "bold"),
            relief="raised", bd=3, state="disabled"
        )
        self.motion_btn.grid(row=3, column=0, columnspan=3, pady=8, ipadx=20, ipady=8)
        self.motion_btn.bind("<ButtonPress-1>",   self._on_motion_press)
        self.motion_btn.bind("<ButtonRelease-1>", self._on_motion_release)

        self.apply_limits_btn = ttk.Button(motion_frame, text="Apply Limits to Motor(s)",
                                           command=self._apply_limits, state="disabled")
        self.apply_limits_btn.grid(row=4, column=0, columnspan=3, pady=4)

        torque_frame = ttk.Frame(motion_frame)
        torque_frame.grid(row=5, column=0, columnspan=3, pady=4)
        self.torque_on_btn  = ttk.Button(torque_frame, text="Torque ON",
                                         command=lambda: self._set_torque(True), state="disabled")
        self.torque_on_btn.pack(side="left", padx=8)
        self.torque_off_btn = ttk.Button(torque_frame, text="Torque OFF",
                                         command=lambda: self._set_torque(False), state="disabled")
        self.torque_off_btn.pack(side="left", padx=8)

        log_frame = ttk.LabelFrame(self.root, text="Log")
        log_frame.grid(row=2, column=0, columnspan=2, sticky="ew", **pad)
        self.log_text = tk.Text(log_frame, height=4, state="disabled", wrap="word")
        log_scroll = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="right", fill="y")

        self.root.columnconfigure(0, weight=1)
        self.root.columnconfigure(1, weight=2)

    def _selected_motor_ids(self) -> list[int]:
        return [mid for mid, var in self._motor_selected.items() if var.get()]

    def _on_motor_selection_changed(self) -> None:
        selected = self._selected_motor_ids()
        for mid, row_widgets in self._param_rows.items():
            if mid in selected:
                for w in row_widgets.values():
                    w.grid()
            else:
                for w in row_widgets.values():
                    w.grid_remove()
        self._rebuild_feedback_table()

    def _rebuild_feedback_table(self) -> None:
        for w in self._fb_inner.winfo_children():
            w.destroy()
        self._fb_labels = {}
        selected_motors = self._selected_motor_ids()
        selected_params = [(l, m) for l, m in FEEDBACK_PARAMS if self._fb_selected[l].get()]
        if not selected_motors or not selected_params:
            ttk.Label(self._fb_inner, text="No motors / parameters selected.").grid(row=0, column=0)
            return
        ttk.Label(self._fb_inner, text="Motor", relief="groove", padding=3, anchor="center").grid(
            row=0, column=0, sticky="ew", padx=1)
        for col, (label, _) in enumerate(selected_params, start=1):
            ttk.Label(self._fb_inner, text=label, relief="groove", padding=3, anchor="center").grid(
                row=0, column=col, sticky="ew", padx=1)
        for row, mid in enumerate(selected_motors, start=1):
            ttk.Label(self._fb_inner, text=f"#{mid}", relief="groove", padding=3, anchor="center").grid(
                row=row, column=0, sticky="ew", padx=1)
            self._fb_labels[mid] = {}
            for col, (label, _) in enumerate(selected_params, start=1):
                lbl = ttk.Label(self._fb_inner, text="--", relief="sunken", padding=3, width=10, anchor="center")
                lbl.grid(row=row, column=col, sticky="ew", padx=1, pady=1)
                self._fb_labels[mid][label] = lbl

    def _update_feedback(self) -> None:
        if self.controller is None:
            return
        selected_motors = self._selected_motor_ids()
        selected_params = [(l, m) for l, m in FEEDBACK_PARAMS if self._fb_selected[l].get()]
        for mid in selected_motors:
            if mid not in self._fb_labels:
                continue
            try:
                servo = self.controller.wrap_servo(mid, verify=False)
                for label, method in selected_params:
                    if label not in self._fb_labels[mid]:
                        continue
                    try:
                        val = getattr(servo.sram, method)()
                        text = str(val) if val is not None else "--"
                    except Exception:
                        text = "ERR"
                    self._fb_labels[mid][label].configure(text=text)
            except Exception:
                pass
        self._feedback_job = self.root.after(FEEDBACK_POLL_MS, self._update_feedback)

    def _toggle_connection(self) -> None:
        if self.controller is None:
            self._connect()
        else:
            self._disconnect()

    def _connect(self) -> None:
        port = self.port_var.get().strip()
        baud = int(self.baud_var.get())
        try:
            self.controller = ST3215(port=port, baudrate=baud)
            self.conn_status.configure(text=f"Connected ({port})", foreground="green")
            self.connect_btn.configure(text="Disconnect")
            self.scan_btn.configure(state="normal")
            self._set_control_state("normal")
            self._log(f"Connected to {port} @ {baud} baud.")
            self._rebuild_feedback_table()
            self._update_feedback()
        except Exception as exc:
            messagebox.showerror("Connection Error", str(exc))
            self._log(f"Connection failed: {exc}")

    def _disconnect(self) -> None:
        self._stop_feedback()
        if self.controller:
            try:
                self.controller.close()
            except Exception:
                pass
            self.controller = None
        self.conn_status.configure(text="Disconnected", foreground="red")
        self.connect_btn.configure(text="Connect")
        self.scan_btn.configure(state="disabled")
        self._set_control_state("disabled")
        self._log("Disconnected.")

    def _stop_feedback(self) -> None:
        if self._feedback_job:
            self.root.after_cancel(self._feedback_job)
            self._feedback_job = None

    def _set_control_state(self, state: str) -> None:
        for btn in [self.apply_limits_btn, self.torque_on_btn,
                    self.torque_off_btn, self.motion_btn]:
            try:
                btn.configure(state=state)
            except tk.TclError:
                pass

    def _scan_motors(self) -> None:
        if self.controller is None:
            return
        self._log("Scanning for motors 1-16 ...")
        def _do_scan():
            found = self.controller.list_servos(start_id=1, end_id=MAX_MOTOR_ID, timeout=0.002)
            self.root.after(0, lambda: self._on_scan_result(found))
        threading.Thread(target=_do_scan, daemon=True).start()

    def _on_scan_result(self, found: list[int]) -> None:
        for mid, var in self._motor_selected.items():
            var.set(mid in found)
        self._on_motor_selection_changed()
        self._log(f"Scan done. Found: {found if found else 'none'}")

    def _apply_limits(self) -> None:
        if self.controller is None:
            return
        for mid in self._selected_motor_ids():
            try:
                servo = self.controller.wrap_servo(mid, verify=False)
                servo.eeprom.write_min_angle_limit(self._min_angle[mid].get())
                servo.eeprom.write_max_angle_limit(self._max_angle[mid].get())
                servo.sram.write_torque_limit(self._torque_limit[mid].get())
                self._log(f"Motor {mid}: limits applied.")
            except Exception as exc:
                self._log(f"Motor {mid}: limit error - {exc}")

    def _set_torque(self, enable: bool) -> None:
        if self.controller is None:
            return
        for mid in self._selected_motor_ids():
            try:
                servo = self.controller.wrap_servo(mid, verify=False)
                if enable:
                    servo.sram.torque_enable()
                else:
                    servo.sram.torque_disable()
            except Exception as exc:
                self._log(f"Motor {mid}: torque error - {exc}")
        self._log(f"Torque {'ON' if enable else 'OFF'} for motors {self._selected_motor_ids()}")

    def _on_motion_press(self, _event=None) -> None:
        if self.controller is None or self._motion_active:
            return
        self._motion_active = True
        self.motion_btn.configure(relief="sunken")
        self._motion_thread = threading.Thread(target=self._motion_loop, daemon=True)
        self._motion_thread.start()

    def _on_motion_release(self, _event=None) -> None:
        self._motion_active = False
        self.motion_btn.configure(relief="raised")
        self.root.after(50, self._go_home)

    def _motion_loop(self) -> None:
        speed  = self.speed_var.get()
        offset = self.offset_var.get()
        direction = self.direction_var.get()
        while self._motion_active and self.controller is not None:
            for mid in self._selected_motor_ids():
                home   = self._home_pos[mid].get()
                target = home + offset if direction == "CW" else home - offset
                target = max(self._min_angle[mid].get(), min(self._max_angle[mid].get(), target))
                try:
                    servo = self.controller.wrap_servo(mid, verify=False)
                    servo.sram.write_running_speed(speed)
                    servo.sram.write_target_location(target)
                except Exception as exc:
                    self.root.after(0, lambda e=exc, m=mid: self._log(f"Motor {m}: motion err - {e}"))
            time.sleep(MOTION_CMD_INTERVAL)

    def _go_home(self) -> None:
        if self.controller is None:
            return
        speed = self.speed_var.get()
        for mid in self._selected_motor_ids():
            home = self._home_pos[mid].get()
            try:
                servo = self.controller.wrap_servo(mid, verify=False)
                servo.sram.write_running_speed(speed)
                servo.sram.write_target_location(home)
            except Exception as exc:
                self._log(f"Motor {mid}: go-home error - {exc}")

    def _log(self, message: str) -> None:
        ts = time.strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{ts}] {message}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def on_close(self) -> None:
        self._motion_active = False
        self._stop_feedback()
        if self.controller:
            try:
                self.controller.close()
            except Exception:
                pass
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    app = ServoGUI(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
