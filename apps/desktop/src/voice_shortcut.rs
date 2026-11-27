//! Foreground-only dictation shortcuts. Leave the original event for AppKit to handle.
use std::{cell::Cell, ptr::NonNull};

use block2::RcBlock;
use objc2_app_kit::{NSEvent, NSEventMask, NSEventModifierFlags, NSEventType};
use tauri::{AppHandle, Emitter, Manager};

pub(crate) fn install(app: AppHandle) {
    const FN_KEY: u16 = 63;
    const V_KEY: u16 = 9;
    let held = Cell::new(false);
    let held_chord = Cell::new(false);
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
        } else if event_type != NSEventType::KeyUp && held.replace(false) {
            Some("cancel")
        } else {
            None
        };
        let chord_flags = NSEventModifierFlags::Control | NSEventModifierFlags::Option;
        let chord = flags.contains(chord_flags)
            && !flags.intersects(NSEventModifierFlags::Command | NSEventModifierFlags::Shift);
        let chord_action = if held_chord.get()
            && ((event_type == NSEventType::KeyUp && key.keyCode() == V_KEY)
                || (event_type == NSEventType::FlagsChanged && !chord))
        {
            held_chord.set(false);
            Some("up")
        } else if event_type == NSEventType::KeyDown
            && key.keyCode() == V_KEY
            && chord
            && !key.isARepeat()
        {
            held_chord.set(true);
            Some("down")
        } else {
            None
        };
        for (event_name, action) in [("voice-fn", action), ("voice-ctrl-alt-v", chord_action)] {
            let Some(action) = action else { continue };
            for label in ["main", "pin"] {
                if let Some(window) = app.get_webview_window(label) {
                    if window.is_focused().unwrap_or(false) {
                        let _ = window.emit(event_name, action);
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
            NSEventMask::FlagsChanged | NSEventMask::KeyDown | NSEventMask::KeyUp,
            &handler,
        );
    }
}
