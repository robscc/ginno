//! Foreground-only Fn events. Leave the original event for AppKit to handle.
use std::{cell::Cell, ptr::NonNull};

use block2::RcBlock;
use objc2_app_kit::{NSEvent, NSEventMask, NSEventModifierFlags, NSEventType};
use tauri::{AppHandle, Emitter, Manager};

pub(crate) fn install(app: AppHandle) {
    const FN_KEY: u16 = 63;
    let held = Cell::new(false);
    let handler = RcBlock::new(move |event: NonNull<NSEvent>| {
        // The AppKit block owns the event for the duration of this callback.
        let key = unsafe { event.as_ref() };
        let flags = key.modifierFlags();
        let function = flags.contains(NSEventModifierFlags::Function);
        let event_type = key.r#type();
        let action = if event_type == NSEventType::FlagsChanged && key.keyCode() == FN_KEY {
            if function {
                held.set(true);
                Some("down")
            } else if held.replace(false) {
                Some("up")
            } else {
                None
            }
        } else if held.replace(false) {
            Some("cancel")
        } else {
            None
        };
        if let Some(action) = action {
            for label in ["main", "pin"] {
                if let Some(window) = app.get_webview_window(label) {
                    if window.is_focused().unwrap_or(false) {
                        let _ = window.emit("voice-fn", action);
                    }
                }
            }
        }
        event.as_ptr()
    });
    // setup runs on the AppKit main thread. The monitor is installed once and
    // lives for the process lifetime, as does its event handler.
    unsafe {
        let _ = NSEvent::addLocalMonitorForEventsMatchingMask_handler(
            NSEventMask::FlagsChanged | NSEventMask::KeyDown,
            &handler,
        );
    }
}
