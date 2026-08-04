from __future__ import annotations

import tkinter as tk


def main() -> None:
    root = tk.Tk()
    root.title("SuperMega Workflow Lab")
    root.geometry("640x360")
    root.minsize(640, 360)

    frame = tk.Frame(root, padx=32, pady=28)
    frame.pack(fill="both", expand=True)

    tk.Label(
        frame,
        text="Local Computer-Use Verification Lab",
        font=("Segoe UI", 18, "bold"),
    ).pack(anchor="w")
    tk.Label(
        frame,
        text="This window writes no files and makes no network requests.",
        font=("Segoe UI", 10),
    ).pack(anchor="w", pady=(4, 24))

    tk.Label(frame, text="Workflow value", font=("Segoe UI", 10)).pack(anchor="w")
    value = tk.Entry(frame, name="workflow_value", font=("Segoe UI", 12))
    value.pack(fill="x", pady=(6, 16), ipady=6)

    status = tk.StringVar(value="Waiting for local workflow")

    def apply() -> None:
        observed = value.get().strip()
        status.set(
            f"Verified locally: {observed}"
            if observed else "Verification failed: value required"
        )
        root.title(
            "SuperMega Workflow Lab - Verified locally"
            if observed else "SuperMega Workflow Lab - Verification failed"
        )

    tk.Button(
        frame,
        text="Apply locally",
        name="apply_locally",
        command=apply,
        font=("Segoe UI", 11, "bold"),
    ).pack(anchor="w", pady=(0, 22), ipadx=12, ipady=5)
    tk.Label(
        frame,
        textvariable=status,
        name="workflow_status",
        font=("Segoe UI", 12, "bold"),
    ).pack(anchor="w")
    value.focus_set()
    root.mainloop()


if __name__ == "__main__":
    main()
