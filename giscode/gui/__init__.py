import tkinter as tk
from giscode.gui.main_window import MainWindow


def _centered_geometry(width: int, height: int, screen_width: int, screen_height: int) -> str:
    x = max(0, (screen_width - width) // 2)
    y = max(0, (screen_height - height) // 2)
    return f"{width}x{height}+{x}+{y}"


def launch_gui(task_manager=None, model_manager=None, coding_agent=None, terminal_executor=None, sandbox_executor=None, orchestrator=None, openhands_adapter=None, knowledge_service=None, routing_service=None, ocr_service=None, monitoring_service=None, packaging_builder=None, signing_service=None, self_improvement_service=None, research_service=None, vision_service=None, project_store=None, chat_store=None, gisai_engine=None, iosxtool_service=None, agent_registry_service=None, agent_runtime_manager=None) -> tk.Tk:
    root = tk.Tk()
    # Never expose the unfinished window: the root stays withdrawn until the
    # whole GisCode interface is built and the laser transition morphs into it.
    root.withdraw()

    if vision_service is None and model_manager is not None:
        from giscode.config.paths import test_mode

        if not test_mode():
            # Discovery reaches the user's LM Studio folders. An isolated test
            # run must not touch them; the explicit switch is what says so.
            try:
                model_manager.auto_register_discovered()
            except Exception:
                pass
        try:
            from giscode.vision.service import VisionService
            vision_service = VisionService(model_manager=model_manager, task_manager=task_manager)
        except Exception:
            vision_service = None
    if gisai_engine is not None and getattr(gisai_engine, "_vision_service", None) is None and vision_service is not None:
        gisai_engine._vision_service = vision_service
        gisai_engine._register_capability_handlers()

    splash = None

    def _reveal_root() -> None:
        # Morph target: the already-built GisCode interface behind the branding,
        # centered on the same point as the Gis artwork, so the faded branding
        # visually becomes the application in one continuous sequence.
        try:
            screen_w = root.winfo_screenwidth()
            screen_h = root.winfo_screenheight()
            root.geometry(_centered_geometry(1400, 900, screen_w, screen_h))
        except Exception:
            pass
        try:
            root.deiconify()
            root.lift()
            root.focus_force()
        except Exception:
            try:
                root.deiconify()
            except Exception:
                pass
        try:
            root.update_idletasks()
        except Exception:
            pass
        if splash is not None:
            try:
                delay = int(
                    splash.HOLD_AFTER_SCAN_MS
                    + splash.FADE_STEPS * splash.FADE_DELTA_MS
                    + 200
                )
                root.after(delay, root.lift)
            except Exception:
                pass

    try:
        from giscode.gui.splash import show_laser_splash
        splash = show_laser_splash(root, on_transition=_reveal_root)
        splash.start()
    except Exception:
        splash = None
        # Graceful degradation: if the branding splash can't be shown, never
        # leave a hidden application window behind.
        try:
            root.deiconify()
        except Exception:
            pass

    try:
        root._giscode_main_window = MainWindow(root, task_manager=task_manager, model_manager=model_manager, coding_agent=coding_agent, terminal_executor=terminal_executor, sandbox_executor=sandbox_executor, orchestrator=orchestrator, openhands_adapter=openhands_adapter, knowledge_service=knowledge_service, routing_service=routing_service, ocr_service=ocr_service, monitoring_service=monitoring_service, packaging_builder=packaging_builder, signing_service=signing_service, self_improvement_service=self_improvement_service, research_service=research_service, vision_service=vision_service, project_store=project_store, chat_store=chat_store, gisai_engine=gisai_engine, iosxtool_service=iosxtool_service, agent_registry_service=agent_registry_service, agent_runtime_manager=agent_runtime_manager)
    except Exception:
        if splash is not None:
            try:
                splash.close()
            except Exception:
                pass
        raise
    return root
