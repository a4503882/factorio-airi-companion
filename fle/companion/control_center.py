"""Tkinter research launcher for AIRI Factorio Companion."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable

from .control_center_backend import (
    ProviderTestResult,
    bridge_status_connected,
    build_bridge_command,
    factorio_process_running,
    mark_bridge_status_stopped,
    read_bridge_status,
    test_provider,
    udp_port_available,
)
from .control_center_store import (
    API_MODES,
    REASONING_EFFORTS,
    ControlCenterStore,
    PromptProfile,
    ProviderProfile,
    SessionFiles,
)
from .credentials import CredentialError, WindowsCredentialStore
from .launcher import discover_factorio_executable, install_mod


WINDOW_TITLE = "AIRI Factorio Control Center"


class ControlCenterApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(WINDOW_TITLE)
        self.geometry("980x760")
        self.minsize(860, 650)
        self.store = ControlCenterStore()
        self.credentials = WindowsCredentialStore()
        self.repo_root = Path(__file__).resolve().parents[2]
        self.events: queue.Queue[tuple[Any, ...]] = queue.Queue()
        self.bridge_process: subprocess.Popen[str] | None = None
        self.factorio_process: subprocess.Popen[Any] | None = None
        self.session: SessionFiles | None = None
        self.current_provider_id: str | None = None
        self.current_prompt_id: str | None = None
        self.provider_display_map: dict[str, str] = {}
        self.prompt_display_map: dict[str, str] = {}
        self._last_external_factorio_check = 0.0
        self._external_factorio_running = False

        self._create_variables()
        self._configure_style()
        self._build_ui()
        self._load_initial_state()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll_events)
        self.after(500, self._poll_status)

    def _create_variables(self) -> None:
        self.provider_choice = tk.StringVar()
        self.provider_name = tk.StringVar()
        self.provider_base_url = tk.StringVar()
        self.provider_model = tk.StringVar()
        self.provider_api_mode = tk.StringVar(value="responses")
        self.provider_web_search = tk.BooleanVar(value=True)
        self.provider_reasoning = tk.StringVar(value="high")
        self.provider_api_key = tk.StringVar()
        self.credential_status = tk.StringVar(value="未保存凭据")

        self.prompt_choice = tk.StringVar()
        self.prompt_name = tk.StringVar()
        self.prompt_hash = tk.StringVar(value="SHA-256：—")

        self.factorio_path = tk.StringVar()
        self.game_udp_port = tk.StringVar(value="31500")
        self.bridge_port = tk.StringVar(value="31501")
        self.install_mod_before_launch = tk.BooleanVar(value=True)
        self.bridge_status = tk.StringVar(value="未启动")
        self.factorio_status = tk.StringVar(value="未运行")
        self.mod_status = tk.StringVar(value="未连接")
        self.session_status = tk.StringVar(value="尚未创建研究会话")

    def _configure_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 16, "bold"))
        style.configure("Section.TLabelframe.Label", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Status.TLabel", font=("Microsoft YaHei UI", 10, "bold"))

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)
        ttk.Label(root, text="AIRI Factorio Control Center", style="Title.TLabel").pack(
            anchor="w"
        )
        ttk.Label(
            root,
            text="Prompt、模型、凭据与实验会话都由本地控制中心管理；API Key 不进入 Mod、存档或命令行。",
        ).pack(anchor="w", pady=(2, 10))

        notebook = ttk.Notebook(root)
        notebook.pack(fill="both", expand=True)
        self.provider_tab = ttk.Frame(notebook, padding=12)
        self.prompt_tab = ttk.Frame(notebook, padding=12)
        self.launch_tab = ttk.Frame(notebook, padding=12)
        self.log_tab = ttk.Frame(notebook, padding=8)
        notebook.add(self.provider_tab, text="Provider / API")
        notebook.add(self.prompt_tab, text="System Prompt")
        notebook.add(self.launch_tab, text="启动与状态")
        notebook.add(self.log_tab, text="实验日志")

        self._build_provider_tab()
        self._build_prompt_tab()
        self._build_launch_tab()
        self._build_log_tab()

    def _build_provider_tab(self) -> None:
        tab = self.provider_tab
        tab.columnconfigure(1, weight=1)

        ttk.Label(tab, text="配置档案").grid(row=0, column=0, sticky="w", pady=5)
        self.provider_combo = ttk.Combobox(
            tab,
            textvariable=self.provider_choice,
            state="readonly",
        )
        self.provider_combo.grid(row=0, column=1, sticky="ew", padx=(8, 8), pady=5)
        self.provider_combo.bind("<<ComboboxSelected>>", self._on_provider_selected)
        profile_buttons = ttk.Frame(tab)
        profile_buttons.grid(row=0, column=2, sticky="e")
        ttk.Button(profile_buttons, text="新建", command=self._new_provider).pack(
            side="left", padx=2
        )
        ttk.Button(profile_buttons, text="导入 ds.txt", command=self._import_provider).pack(
            side="left", padx=2
        )
        ttk.Button(profile_buttons, text="删除", command=self._delete_provider).pack(
            side="left", padx=2
        )

        fields = [
            ("档案名称", self.provider_name),
            ("Base URL", self.provider_base_url),
            ("Model", self.provider_model),
        ]
        row = 1
        for label, variable in fields:
            ttk.Label(tab, text=label).grid(row=row, column=0, sticky="w", pady=5)
            ttk.Entry(tab, textvariable=variable).grid(
                row=row,
                column=1,
                columnspan=2,
                sticky="ew",
                padx=(8, 0),
                pady=5,
            )
            row += 1

        ttk.Label(tab, text="API 协议").grid(row=row, column=0, sticky="w", pady=5)
        mode_frame = ttk.Frame(tab)
        mode_frame.grid(row=row, column=1, columnspan=2, sticky="w", padx=(8, 0))
        ttk.Combobox(
            mode_frame,
            textvariable=self.provider_api_mode,
            values=API_MODES,
            state="readonly",
            width=22,
        ).pack(side="left")
        ttk.Checkbutton(
            mode_frame,
            text="启用原生 Web Search",
            variable=self.provider_web_search,
        ).pack(side="left", padx=(16, 0))
        row += 1

        ttk.Label(tab, text="Reasoning effort").grid(
            row=row, column=0, sticky="w", pady=5
        )
        ttk.Combobox(
            tab,
            textvariable=self.provider_reasoning,
            values=REASONING_EFFORTS,
            state="readonly",
            width=22,
        ).grid(row=row, column=1, sticky="w", padx=(8, 0), pady=5)
        row += 1

        credential = ttk.LabelFrame(
            tab,
            text="Windows Credential Manager",
            style="Section.TLabelframe",
            padding=10,
        )
        credential.grid(
            row=row,
            column=0,
            columnspan=3,
            sticky="ew",
            pady=(14, 8),
        )
        credential.columnconfigure(1, weight=1)
        ttk.Label(credential, text="新 API Key").grid(row=0, column=0, sticky="w")
        ttk.Entry(
            credential,
            textvariable=self.provider_api_key,
            show="●",
        ).grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Label(credential, textvariable=self.credential_status).grid(
            row=1,
            column=1,
            sticky="w",
            padx=8,
            pady=(6, 0),
        )
        ttk.Button(
            credential,
            text="删除凭据",
            command=self._delete_credential,
        ).grid(row=0, column=2, padx=(4, 0))
        row += 1

        actions = ttk.Frame(tab)
        actions.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        ttk.Button(actions, text="保存配置档案", command=self._save_provider).pack(
            side="left"
        )
        ttk.Button(actions, text="测试 API", command=self._test_api).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(actions, text="测试原生搜索", command=self._test_search).pack(
            side="left", padx=(8, 0)
        )
        ttk.Label(
            tab,
            text=(
                "密钥保存后输入框会立即清空。测试原生搜索可能产生多次模型调用和额外 token。"
            ),
            foreground="#666666",
        ).grid(row=row + 1, column=0, columnspan=3, sticky="w", pady=(12, 0))

    def _build_prompt_tab(self) -> None:
        tab = self.prompt_tab
        tab.columnconfigure(1, weight=1)
        tab.rowconfigure(2, weight=1)
        ttk.Label(tab, text="Prompt 预设").grid(row=0, column=0, sticky="w", pady=5)
        self.prompt_combo = ttk.Combobox(
            tab,
            textvariable=self.prompt_choice,
            state="readonly",
        )
        self.prompt_combo.grid(row=0, column=1, sticky="ew", padx=8, pady=5)
        self.prompt_combo.bind("<<ComboboxSelected>>", self._on_prompt_selected)
        prompt_buttons = ttk.Frame(tab)
        prompt_buttons.grid(row=0, column=2, sticky="e")
        ttk.Button(prompt_buttons, text="新建", command=self._new_prompt).pack(
            side="left", padx=2
        )
        ttk.Button(prompt_buttons, text="复制", command=self._duplicate_prompt).pack(
            side="left", padx=2
        )
        ttk.Button(prompt_buttons, text="删除", command=self._delete_prompt).pack(
            side="left", padx=2
        )

        ttk.Label(tab, text="预设名称").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Entry(tab, textvariable=self.prompt_name).grid(
            row=1,
            column=1,
            columnspan=2,
            sticky="ew",
            padx=(8, 0),
            pady=5,
        )

        prompt_frame = ttk.Frame(tab)
        prompt_frame.grid(
            row=2,
            column=0,
            columnspan=3,
            sticky="nsew",
            pady=(8, 8),
        )
        prompt_frame.columnconfigure(0, weight=1)
        prompt_frame.rowconfigure(0, weight=1)
        self.prompt_text = tk.Text(
            prompt_frame,
            wrap="word",
            undo=True,
            font=("Consolas", 10),
        )
        prompt_scroll = ttk.Scrollbar(
            prompt_frame,
            orient="vertical",
            command=self.prompt_text.yview,
        )
        self.prompt_text.configure(yscrollcommand=prompt_scroll.set)
        self.prompt_text.grid(row=0, column=0, sticky="nsew")
        prompt_scroll.grid(row=0, column=1, sticky="ns")

        footer = ttk.Frame(tab)
        footer.grid(row=3, column=0, columnspan=3, sticky="ew")
        ttk.Button(footer, text="保存 Prompt", command=self._save_prompt).pack(
            side="left"
        )
        ttk.Label(footer, textvariable=self.prompt_hash).pack(side="left", padx=12)
        ttk.Label(
            footer,
            text="正在运行的 Bridge 使用会话快照；修改会在下次启动 Bridge 时生效。",
            foreground="#666666",
        ).pack(side="right")

    def _build_launch_tab(self) -> None:
        tab = self.launch_tab
        tab.columnconfigure(1, weight=1)
        ttk.Label(tab, text="Factorio 可执行文件").grid(
            row=0,
            column=0,
            sticky="w",
            pady=5,
        )
        ttk.Entry(tab, textvariable=self.factorio_path).grid(
            row=0,
            column=1,
            sticky="ew",
            padx=8,
            pady=5,
        )
        ttk.Button(tab, text="浏览", command=self._browse_factorio).grid(
            row=0,
            column=2,
        )

        ports = ttk.Frame(tab)
        ports.grid(row=1, column=0, columnspan=3, sticky="w", pady=8)
        ttk.Label(ports, text="Factorio UDP 端口").pack(side="left")
        ttk.Entry(ports, textvariable=self.game_udp_port, width=8).pack(
            side="left", padx=(8, 22)
        )
        ttk.Label(ports, text="Bridge 端口").pack(side="left")
        ttk.Entry(ports, textvariable=self.bridge_port, width=8).pack(
            side="left", padx=8
        )
        ttk.Checkbutton(
            ports,
            text="启动前更新 AIRI Mod",
            variable=self.install_mod_before_launch,
        ).pack(side="left", padx=(20, 0))
        ttk.Label(
            tab,
            text="若修改 Bridge 端口，Factorio 的“AIRI bridge UDP port”Mod 设置必须使用同一值。",
            foreground="#666666",
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(0, 6))

        status = ttk.LabelFrame(
            tab,
            text="运行状态",
            style="Section.TLabelframe",
            padding=12,
        )
        status.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(10, 8))
        status.columnconfigure((1, 3, 5), weight=1)
        ttk.Label(status, text="Bridge").grid(row=0, column=0, sticky="w")
        ttk.Label(status, textvariable=self.bridge_status, style="Status.TLabel").grid(
            row=0, column=1, sticky="w", padx=(8, 18)
        )
        ttk.Label(status, text="Factorio").grid(row=0, column=2, sticky="w")
        ttk.Label(status, textvariable=self.factorio_status, style="Status.TLabel").grid(
            row=0, column=3, sticky="w", padx=(8, 18)
        )
        ttk.Label(status, text="Mod").grid(row=0, column=4, sticky="w")
        ttk.Label(status, textvariable=self.mod_status, style="Status.TLabel").grid(
            row=0, column=5, sticky="w", padx=(8, 0)
        )
        ttk.Label(status, textvariable=self.session_status).grid(
            row=1,
            column=0,
            columnspan=6,
            sticky="w",
            pady=(10, 0),
        )

        actions = ttk.Frame(tab)
        actions.grid(row=4, column=0, columnspan=3, sticky="w", pady=10)
        ttk.Button(actions, text="启动 Bridge", command=self._start_bridge).pack(
            side="left"
        )
        ttk.Button(actions, text="启动 Factorio", command=self._start_factorio).pack(
            side="left", padx=8
        )
        ttk.Button(actions, text="全部启动", command=self._start_all).pack(
            side="left"
        )
        ttk.Button(actions, text="停止 Bridge", command=self._stop_bridge).pack(
            side="left", padx=8
        )
        ttk.Button(actions, text="打开会话目录", command=self._open_session_dir).pack(
            side="left"
        )

        explanation = (
            "每次启动 Bridge 都会创建一个新研究会话，并冻结当前 Prompt、Provider 参数、"
            "Prompt SHA-256、事件轨迹和 Bridge 日志。API Key 不写入这些文件。"
        )
        ttk.Label(tab, text=explanation, wraplength=820, foreground="#555555").grid(
            row=5,
            column=0,
            columnspan=3,
            sticky="w",
            pady=(10, 0),
        )

    def _build_log_tab(self) -> None:
        tab = self.log_tab
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(0, weight=1)
        self.log_text = tk.Text(
            tab,
            state="disabled",
            wrap="word",
            font=("Consolas", 9),
        )
        scrollbar = ttk.Scrollbar(tab, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        ttk.Button(tab, text="清空显示", command=self._clear_log_display).grid(
            row=1,
            column=0,
            sticky="w",
            pady=(8, 0),
        )

    def _load_initial_state(self) -> None:
        settings = self.store.load_settings()
        self.factorio_path.set(str(settings.get("factorio_executable") or ""))
        self.game_udp_port.set(str(settings.get("game_udp_port") or 31500))
        self.bridge_port.set(str(settings.get("bridge_port") or 31501))
        self.install_mod_before_launch.set(
            bool(settings.get("install_mod_before_launch", True))
        )
        if not self.factorio_path.get():
            try:
                self.factorio_path.set(str(discover_factorio_executable()))
            except FileNotFoundError:
                pass
        self._reload_provider_choices(settings.get("selected_provider_id"))
        self._reload_prompt_choices(settings.get("selected_prompt_id"))
        self._append_log(f"配置目录：{self.store.root}")

    def _append_log(self, text: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{timestamp}] {text.rstrip()}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _clear_log_display(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _reload_provider_choices(self, select_id: str | None = None) -> None:
        profiles = self.store.load_providers()
        self.provider_display_map = {
            f"{profile.name}  [{profile.model}]  ({profile.id[-6:]})": profile.id
            for profile in profiles
        }
        values = list(self.provider_display_map)
        self.provider_combo.configure(values=values)
        if not profiles:
            self._new_provider()
            return
        selected = next(
            (profile for profile in profiles if profile.id == select_id),
            profiles[0],
        )
        display = next(
            display
            for display, profile_id in self.provider_display_map.items()
            if profile_id == selected.id
        )
        self.provider_choice.set(display)
        self._load_provider(selected)

    def _on_provider_selected(self, _event: tk.Event[Any] | None = None) -> None:
        profile_id = self.provider_display_map.get(self.provider_choice.get())
        profile = next(
            (item for item in self.store.load_providers() if item.id == profile_id),
            None,
        )
        if profile is not None:
            self._load_provider(profile)

    def _load_provider(self, profile: ProviderProfile) -> None:
        self.current_provider_id = profile.id
        self.provider_name.set(profile.name)
        self.provider_base_url.set(profile.base_url)
        self.provider_model.set(profile.model)
        self.provider_api_mode.set(profile.api_mode)
        self.provider_web_search.set(profile.web_search)
        self.provider_reasoning.set(profile.reasoning_effort)
        self.provider_api_key.set("")
        self._refresh_credential_status(profile)
        self.store.save_settings(selected_provider_id=profile.id)

    def _new_provider(self) -> None:
        self.current_provider_id = None
        self.provider_choice.set("")
        self.provider_name.set("DeepSeek Responses")
        self.provider_base_url.set("https://api.deepseek.com")
        self.provider_model.set("deepseek-v4-flash")
        self.provider_api_mode.set("responses")
        self.provider_web_search.set(True)
        self.provider_reasoning.set("high")
        self.provider_api_key.set("")
        self.credential_status.set("尚未保存配置档案或凭据")

    def _collect_provider(self) -> ProviderProfile:
        existing = next(
            (
                profile
                for profile in self.store.load_providers()
                if profile.id == self.current_provider_id
            ),
            None,
        )
        if existing is None:
            return ProviderProfile.create(
                name=self.provider_name.get(),
                base_url=self.provider_base_url.get(),
                model=self.provider_model.get(),
                api_mode=self.provider_api_mode.get(),
                web_search=self.provider_web_search.get(),
                reasoning_effort=self.provider_reasoning.get(),
            )
        value = asdict(existing)
        value.update(
            {
                "name": self.provider_name.get().strip(),
                "base_url": self.provider_base_url.get().rstrip("/"),
                "model": self.provider_model.get().strip(),
                "api_mode": self.provider_api_mode.get(),
                "web_search": self.provider_web_search.get(),
                "reasoning_effort": self.provider_reasoning.get(),
            }
        )
        return ProviderProfile(**value).validated()

    def _save_provider(self, *, notify: bool = True) -> ProviderProfile | None:
        try:
            profile = self.store.save_provider(self._collect_provider())
            api_key = self.provider_api_key.get().strip()
            if api_key:
                self.credentials.set_secret(profile.credential_id, api_key)
                self.provider_api_key.set("")
            self.current_provider_id = profile.id
            self._reload_provider_choices(profile.id)
            if notify:
                self._append_log(f"已保存 Provider：{profile.name}")
            return profile
        except (ValueError, RuntimeError, CredentialError) as exc:
            messagebox.showerror("Provider 配置无效", str(exc), parent=self)
            return None

    def _delete_provider(self) -> None:
        if not self.current_provider_id:
            return
        if not messagebox.askyesno(
            "删除 Provider",
            "将同时删除对应的 Windows 凭据。确定继续吗？",
            parent=self,
        ):
            return
        try:
            self.store.delete_provider(self.current_provider_id, self.credentials)
        except (RuntimeError, CredentialError) as exc:
            messagebox.showerror("删除失败", str(exc), parent=self)
            return
        self.current_provider_id = None
        self._reload_provider_choices()
        self._append_log("Provider 和对应凭据已删除")

    def _import_provider(self) -> None:
        initial = Path(r"F:\ds.txt")
        path = filedialog.askopenfilename(
            parent=self,
            title="导入三行 Provider 配置",
            initialdir=str(initial.parent) if initial.parent.exists() else None,
            initialfile=initial.name,
            filetypes=(("Text files", "*.txt"), ("All files", "*.*")),
        )
        if not path:
            return
        try:
            profile = self.store.import_three_line_provider(path, self.credentials)
        except (RuntimeError, ValueError, CredentialError) as exc:
            messagebox.showerror("导入失败", str(exc), parent=self)
            return
        self._reload_provider_choices(profile.id)
        self._append_log(
            f"已导入 Provider：{profile.name}；密钥已转存到 Windows Credential Manager"
        )

    def _refresh_credential_status(self, profile: ProviderProfile) -> None:
        try:
            exists = self.credentials.get_secret(profile.credential_id) is not None
        except CredentialError as exc:
            self.credential_status.set(str(exc))
            return
        self.credential_status.set(
            "凭据已安全保存（不会显示原文）" if exists else "尚未保存 API Key"
        )

    def _delete_credential(self) -> None:
        profile = self._selected_provider()
        if profile is None:
            return
        if not messagebox.askyesno(
            "删除凭据",
            "确定从 Windows Credential Manager 删除此 API Key 吗？",
            parent=self,
        ):
            return
        try:
            deleted = self.credentials.delete_secret(profile.credential_id)
        except CredentialError as exc:
            messagebox.showerror("删除失败", str(exc), parent=self)
            return
        self._refresh_credential_status(profile)
        self._append_log("API 凭据已删除" if deleted else "API 凭据原本就不存在")

    def _selected_provider(self) -> ProviderProfile | None:
        return next(
            (
                profile
                for profile in self.store.load_providers()
                if profile.id == self.current_provider_id
            ),
            None,
        )

    def _test_api(self) -> None:
        self._run_provider_test(native_search=False)

    def _test_search(self) -> None:
        if not messagebox.askyesno(
            "测试原生搜索",
            "此测试可能产生多次模型调用和额外 token，是否继续？",
            parent=self,
        ):
            return
        self._run_provider_test(native_search=True)

    def _run_provider_test(self, *, native_search: bool) -> None:
        profile = self._save_provider(notify=False)
        if profile is None:
            return
        try:
            api_key = self.credentials.get_secret(profile.credential_id)
        except CredentialError as exc:
            messagebox.showerror("凭据读取失败", str(exc), parent=self)
            return
        if not api_key:
            messagebox.showerror("缺少 API Key", "请先保存 API Key。", parent=self)
            return
        label = "原生搜索测试" if native_search else "API 连接测试"
        self._append_log(f"开始{label}：{profile.name}")

        def task() -> ProviderTestResult:
            return test_provider(profile, api_key, native_search=native_search)

        def completed(result: ProviderTestResult) -> None:
            details = [
                f"状态：{result.status}",
                f"模型：{result.model}",
            ]
            if native_search:
                details.append(f"web_search_call：{result.web_search_calls}")
            if result.text:
                details.append(f"回复：{result.text[:800]}")
            self._append_log(f"{label}通过：" + "；".join(details[:3]))
            messagebox.showinfo(label, "\n\n".join(details), parent=self)

        self._run_async(label, task, completed)

    def _reload_prompt_choices(self, select_id: str | None = None) -> None:
        prompts = self.store.load_prompts()
        self.prompt_display_map = {
            f"{prompt.name}  [{prompt.sha256[:8]}]  ({prompt.id[-6:]})": prompt.id
            for prompt in prompts
        }
        values = list(self.prompt_display_map)
        self.prompt_combo.configure(values=values)
        selected = next(
            (prompt for prompt in prompts if prompt.id == select_id),
            prompts[0],
        )
        display = next(
            display
            for display, prompt_id in self.prompt_display_map.items()
            if prompt_id == selected.id
        )
        self.prompt_choice.set(display)
        self._load_prompt(selected)

    def _on_prompt_selected(self, _event: tk.Event[Any] | None = None) -> None:
        prompt_id = self.prompt_display_map.get(self.prompt_choice.get())
        prompt = next(
            (item for item in self.store.load_prompts() if item.id == prompt_id),
            None,
        )
        if prompt is not None:
            self._load_prompt(prompt)

    def _load_prompt(self, prompt: PromptProfile) -> None:
        self.current_prompt_id = prompt.id
        self.prompt_name.set(prompt.name)
        self.prompt_text.delete("1.0", "end")
        self.prompt_text.insert("1.0", prompt.system_prompt)
        self.prompt_hash.set(f"SHA-256：{prompt.sha256}")
        self.store.save_settings(selected_prompt_id=prompt.id)

    def _new_prompt(self) -> None:
        self.current_prompt_id = None
        self.prompt_choice.set("")
        self.prompt_name.set("新 Prompt 实验")
        self.prompt_text.delete("1.0", "end")
        self.prompt_text.insert("1.0", "You are AIRI, an in-game Factorio companion.\n")
        self.prompt_hash.set("SHA-256：尚未保存")

    def _duplicate_prompt(self) -> None:
        text = self.prompt_text.get("1.0", "end-1c")
        name = self.prompt_name.get().strip() or "Prompt"
        self.current_prompt_id = None
        self.prompt_choice.set("")
        self.prompt_name.set(name + " 副本")
        self.prompt_text.delete("1.0", "end")
        self.prompt_text.insert("1.0", text)
        self.prompt_hash.set("SHA-256：尚未保存")

    def _save_prompt(self, *, notify: bool = True) -> PromptProfile | None:
        text = self.prompt_text.get("1.0", "end-1c")
        existing = next(
            (
                prompt
                for prompt in self.store.load_prompts()
                if prompt.id == self.current_prompt_id
            ),
            None,
        )
        try:
            if existing is None:
                prompt = PromptProfile.create(
                    name=self.prompt_name.get(),
                    system_prompt=text,
                )
            else:
                value = asdict(existing)
                value.update(
                    {
                        "name": self.prompt_name.get().strip(),
                        "system_prompt": text.strip(),
                    }
                )
                prompt = PromptProfile(**value).validated()
            prompt = self.store.save_prompt(prompt)
        except (ValueError, RuntimeError) as exc:
            messagebox.showerror("Prompt 无效", str(exc), parent=self)
            return None
        self.current_prompt_id = prompt.id
        self._reload_prompt_choices(prompt.id)
        if notify:
            self._append_log(f"已保存 Prompt：{prompt.name} ({prompt.sha256[:12]})")
        return prompt

    def _delete_prompt(self) -> None:
        if not self.current_prompt_id:
            return
        if not messagebox.askyesno(
            "删除 Prompt",
            "确定删除这个 Prompt 预设吗？已有会话快照不会受影响。",
            parent=self,
        ):
            return
        try:
            self.store.delete_prompt(self.current_prompt_id)
        except RuntimeError as exc:
            messagebox.showerror("删除失败", str(exc), parent=self)
            return
        self.current_prompt_id = None
        self._reload_prompt_choices()
        self._append_log("Prompt 预设已删除")

    def _selected_prompt(self) -> PromptProfile | None:
        return next(
            (
                prompt
                for prompt in self.store.load_prompts()
                if prompt.id == self.current_prompt_id
            ),
            None,
        )

    def _browse_factorio(self) -> None:
        path = filedialog.askopenfilename(
            parent=self,
            title="选择 Factorio.exe",
            filetypes=(("Factorio", "factorio.exe"), ("Executable", "*.exe")),
        )
        if path:
            self.factorio_path.set(path)

    def _validated_ports(self) -> tuple[int, int] | None:
        try:
            game_port = int(self.game_udp_port.get())
            bridge_port = int(self.bridge_port.get())
        except ValueError:
            messagebox.showerror("端口无效", "UDP 端口必须是整数。", parent=self)
            return None
        if not (1024 <= game_port <= 65535 and 1024 <= bridge_port <= 65535):
            messagebox.showerror(
                "端口无效",
                "UDP 端口必须在 1024 到 65535 之间。",
                parent=self,
            )
            return None
        if game_port == bridge_port:
            messagebox.showerror("端口冲突", "Factorio 与 Bridge 端口不能相同。", parent=self)
            return None
        return game_port, bridge_port

    def _save_launch_settings(self, game_port: int, bridge_port: int) -> None:
        self.store.save_settings(
            factorio_executable=self.factorio_path.get().strip(),
            game_udp_port=game_port,
            bridge_port=bridge_port,
            install_mod_before_launch=self.install_mod_before_launch.get(),
        )

    def _start_bridge(self) -> bool:
        if self.bridge_process is not None and self.bridge_process.poll() is None:
            messagebox.showinfo("Bridge 已运行", "当前 Bridge 已经在运行。", parent=self)
            return True
        ports = self._validated_ports()
        if ports is None:
            return False
        game_port, bridge_port = ports
        if not udp_port_available(bridge_port):
            messagebox.showerror(
                "Bridge 端口被占用",
                f"127.0.0.1:{bridge_port} 已被其他程序占用。",
                parent=self,
            )
            return False
        provider = self._save_provider(notify=False)
        prompt = self._save_prompt(notify=False)
        if provider is None or prompt is None:
            return False
        try:
            api_key = self.credentials.get_secret(provider.credential_id)
        except CredentialError as exc:
            messagebox.showerror("凭据读取失败", str(exc), parent=self)
            return False
        if not api_key:
            messagebox.showerror(
                "缺少 API Key",
                "所选 Provider 没有保存 API Key。",
                parent=self,
            )
            return False
        factorio_executable = self.factorio_path.get().strip()
        try:
            session = self.store.create_session(
                provider=provider,
                prompt=prompt,
                factorio_executable=factorio_executable,
                game_udp_port=game_port,
                bridge_port=bridge_port,
            )
            command = build_bridge_command(
                provider,
                session,
                bridge_port=bridge_port,
                game_udp_port=game_port,
            )
            environment = os.environ.copy()
            environment["AIRI_FACTORIO_API_KEY"] = api_key
            environment["PYTHONIOENCODING"] = "utf-8"
            environment["PYTHONUNBUFFERED"] = "1"
            creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            process = subprocess.Popen(
                command,
                cwd=str(self.repo_root),
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creation_flags,
            )
            environment.pop("AIRI_FACTORIO_API_KEY", None)
            self.bridge_process = process
            self.session = session
            self.store.update_session(
                session,
                status="bridge_starting",
                bridge_pid=process.pid,
            )
            self._save_launch_settings(game_port, bridge_port)
            self._start_bridge_reader(process, session)
            self._append_log(
                f"Bridge 启动中：PID {process.pid}；会话 {session.session_id}"
            )
            self.session_status.set(f"当前会话：{session.session_id}")
            return True
        except (OSError, RuntimeError, ValueError) as exc:
            messagebox.showerror("Bridge 启动失败", str(exc), parent=self)
            return False

    def _start_bridge_reader(
        self,
        process: subprocess.Popen[str],
        session: SessionFiles,
    ) -> None:
        def reader() -> None:
            try:
                with session.bridge_log.open(
                    "a",
                    encoding="utf-8",
                    newline="\n",
                ) as log_stream:
                    if process.stdout is not None:
                        for line in process.stdout:
                            log_stream.write(line)
                            log_stream.flush()
                            self.events.put(("log", "Bridge | " + line.rstrip()))
                exit_code = process.wait()
            except Exception as exc:
                self.events.put(("log", f"Bridge 日志读取失败：{exc}"))
                exit_code = process.poll()
            self.events.put(("bridge_exit", process, session, exit_code))

        threading.Thread(target=reader, daemon=True, name="airi-bridge-log").start()

    def _start_factorio(self) -> bool:
        if self.factorio_process is not None and self.factorio_process.poll() is None:
            messagebox.showinfo("Factorio 已运行", "控制中心启动的 Factorio 已在运行。", parent=self)
            return True
        if factorio_process_running():
            self._append_log("检测到现有 Factorio 进程；未重复启动")
            messagebox.showinfo(
                "Factorio 已运行",
                "检测到现有 Factorio。若它带有 --enable-lua-udp 参数，Bridge 会自动连接。",
                parent=self,
            )
            return True
        ports = self._validated_ports()
        if ports is None:
            return False
        game_port, bridge_port = ports
        try:
            executable = discover_factorio_executable(self.factorio_path.get().strip())
            self.factorio_path.set(str(executable))
            if self.install_mod_before_launch.get():
                installed = install_mod()
                self._append_log(f"已更新 AIRI Mod：{installed}")
            command = [str(executable), "--enable-lua-udp", str(game_port)]
            process = subprocess.Popen(command, cwd=str(executable.parent))
            self.factorio_process = process
            self._save_launch_settings(game_port, bridge_port)
            if self.session is not None:
                self.store.update_session(
                    self.session,
                    status="factorio_starting",
                    factorio_pid=process.pid,
                )
            self._append_log(
                f"Factorio 已启动：PID {process.pid}；--enable-lua-udp {game_port}"
            )

            def wait_for_game() -> None:
                exit_code = process.wait()
                self.events.put(("factorio_exit", process, exit_code))

            threading.Thread(
                target=wait_for_game,
                daemon=True,
                name="airi-factorio-wait",
            ).start()
            return True
        except (OSError, RuntimeError, ValueError) as exc:
            messagebox.showerror("Factorio 启动失败", str(exc), parent=self)
            return False

    def _start_all(self) -> None:
        if self._start_bridge():
            self.after(700, self._start_factorio)

    def _stop_bridge(self) -> None:
        process = self.bridge_process
        if process is None or process.poll() is not None:
            self._append_log("Bridge 当前未运行")
            return
        self._append_log(f"正在停止 Bridge PID {process.pid}")

        def stop() -> None:
            try:
                process.terminate()
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            except OSError as exc:
                self.events.put(("log", f"停止 Bridge 失败：{exc}"))

        threading.Thread(target=stop, daemon=True, name="airi-bridge-stop").start()

    def _open_session_dir(self) -> None:
        if self.session is None:
            messagebox.showinfo("没有会话", "尚未创建研究会话。", parent=self)
            return
        try:
            os.startfile(self.session.directory)  # type: ignore[attr-defined]
        except OSError as exc:
            messagebox.showerror("无法打开目录", str(exc), parent=self)

    def _run_async(
        self,
        label: str,
        function: Callable[[], Any],
        completed: Callable[[Any], None],
    ) -> None:
        def worker() -> None:
            try:
                result = function()
            except Exception as exc:
                self.events.put(("task_error", label, str(exc)))
                return
            self.events.put(("task_success", label, completed, result))

        threading.Thread(target=worker, daemon=True, name="airi-control-task").start()

    def _poll_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "log":
                    self._append_log(str(event[1]))
                elif kind == "task_error":
                    _, label, error = event
                    self._append_log(f"{label}失败：{error}")
                    messagebox.showerror(f"{label}失败", error, parent=self)
                elif kind == "task_success":
                    _, _label, callback, result = event
                    callback(result)
                elif kind == "bridge_exit":
                    _, process, session, exit_code = event
                    self._append_log(f"Bridge 已退出，代码 {exit_code}")
                    try:
                        mark_bridge_status_stopped(session.status_file, exit_code)
                        self.store.update_session(
                            session,
                            status="bridge_stopped",
                            bridge_exit_code=exit_code,
                            stopped_at=datetime.now().astimezone().isoformat(
                                timespec="seconds"
                            ),
                        )
                    except RuntimeError as exc:
                        self._append_log(f"更新会话清单失败：{exc}")
                    if self.bridge_process is process:
                        self.bridge_process = None
                elif kind == "factorio_exit":
                    _, process, exit_code = event
                    self._append_log(f"Factorio 已退出，代码 {exit_code}")
                    if self.factorio_process is process:
                        self.factorio_process = None
        except queue.Empty:
            pass
        self.after(100, self._poll_events)

    def _poll_status(self) -> None:
        bridge_running = (
            self.bridge_process is not None and self.bridge_process.poll() is None
        )
        status: dict[str, Any] = {}
        if self.session is not None:
            status = read_bridge_status(self.session.status_file)
        connected = bridge_status_connected(status)
        if bridge_running:
            self.bridge_status.set("运行中")
            self.mod_status.set("已连接" if connected else "等待 Factorio")
        else:
            self.bridge_status.set("未启动")
            self.mod_status.set("未连接")

        now = time.monotonic()
        if now - self._last_external_factorio_check >= 2.0:
            self._external_factorio_running = factorio_process_running()
            self._last_external_factorio_check = now
        managed_running = (
            self.factorio_process is not None and self.factorio_process.poll() is None
        )
        if managed_running:
            self.factorio_status.set("运行中（控制中心）")
        elif self._external_factorio_running:
            self.factorio_status.set("运行中（外部启动）")
        else:
            self.factorio_status.set("未运行")
        self.after(500, self._poll_status)

    def _on_close(self) -> None:
        process = self.bridge_process
        if process is not None and process.poll() is None:
            if not messagebox.askyesno(
                "退出 Control Center",
                "退出会停止 AgentBridge，但不会关闭 Factorio。确定继续吗？",
                parent=self,
            ):
                return
            try:
                process.terminate()
                process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                except OSError:
                    pass
        self.destroy()


def main() -> int:
    try:
        app = ControlCenterApp()
    except (RuntimeError, CredentialError, tk.TclError) as exc:
        print(f"AIRI Control Center could not start: {exc}", file=sys.stderr)
        return 1
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
