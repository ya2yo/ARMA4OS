pub mod config;
pub mod error;
pub mod executor;
pub mod lock;
pub mod protocol;
pub mod sysfs;

use std::io::{BufRead, Write};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{sync_channel, RecvTimeoutError};
use std::sync::Arc;
use std::thread;
use std::time::Duration;

use crate::error::ExecutorError;
use crate::executor::ArmaExecutor;
use crate::protocol::ProtocolResponse;

pub const MAX_LINE_BYTES: usize = 1_048_576; // 1 MiB bounded input lines

/// Reads a single line bounded by `max_bytes` into `buf`.
/// Drains remaining bytes if the line exceeds limit so subsequent lines remain synchronized.
pub fn read_bounded_line<R: BufRead>(
    reader: &mut R,
    buf: &mut String,
    max_bytes: usize,
) -> Result<Option<usize>, ExecutorError> {
    buf.clear();
    let mut total = 0;
    loop {
        let available = reader.fill_buf()?;
        if available.is_empty() {
            if total == 0 {
                return Ok(None);
            }
            return Ok(Some(total));
        }

        if let Some(pos) = available.iter().position(|&b| b == b'\n') {
            let take = pos + 1;
            if total + take > max_bytes {
                reader.consume(take);
                return Err(ExecutorError::InputLineTooLong(format!(
                    "Input line exceeds maximum limit of {} bytes",
                    max_bytes
                )));
            }
            let s = std::str::from_utf8(&available[..take])
                .map_err(|e| ExecutorError::Internal(format!("Invalid UTF-8 in input: {}", e)))?;
            buf.push_str(s);
            reader.consume(take);
            total += take;
            return Ok(Some(total));
        } else {
            let len = available.len();
            if total + len > max_bytes {
                reader.consume(len);
                // Drain remainder of line until newline or EOF
                loop {
                    let next_avail = reader.fill_buf()?;
                    if next_avail.is_empty() {
                        break;
                    }
                    if let Some(nl) = next_avail.iter().position(|&b| b == b'\n') {
                        reader.consume(nl + 1);
                        break;
                    } else {
                        let drain_len = next_avail.len();
                        reader.consume(drain_len);
                    }
                }
                return Err(ExecutorError::InputLineTooLong(format!(
                    "Input line exceeds maximum limit of {} bytes",
                    max_bytes
                )));
            }
            let s = std::str::from_utf8(available)
                .map_err(|e| ExecutorError::Internal(format!("Invalid UTF-8 in input: {}", e)))?;
            buf.push_str(s);
            reader.consume(len);
            total += len;
        }
    }
}

/// Runs the executor message loop reading lines from a BufRead and writing responses to a Write.
/// Used for synchronous processing or testing.
pub fn run_executor_sync<R: BufRead, W: Write>(
    mut executor: ArmaExecutor,
    mut reader: R,
    mut writer: W,
    shutdown_flag: Arc<AtomicBool>,
) -> Result<(), ExecutorError> {
    let mut line = String::new();
    loop {
        if shutdown_flag.load(Ordering::SeqCst) {
            eprintln!("Shutdown requested, cleaning up...");
            executor.clean_up();
            break;
        }

        line.clear();
        match read_bounded_line(&mut reader, &mut line, MAX_LINE_BYTES) {
            Ok(None) => {
                // EOF
                eprintln!("Input reached EOF, cleaning up...");
                executor.clean_up();
                break;
            }
            Ok(Some(_)) => {
                let resp = executor.process_raw_request(&line);
                let resp_json =
                    serde_json::to_string(&resp).map_err(crate::error::ConfigError::from)?;
                if let Err(e) = writeln!(writer, "{}", resp_json).and_then(|_| writer.flush()) {
                    eprintln!("Failed to write to stdout/writer: {}", e);
                    executor.clean_up();
                    return Err(ExecutorError::Io(e));
                }

                if executor.is_shut_down() {
                    eprintln!("Shutdown request executed, exiting loop...");
                    break;
                }
            }
            Err(ExecutorError::InputLineTooLong(msg)) => {
                let resp = ProtocolResponse::error("unknown".to_string(), "INVALID_REQUEST", msg);
                let resp_json =
                    serde_json::to_string(&resp).map_err(crate::error::ConfigError::from)?;
                if let Err(e) = writeln!(writer, "{}", resp_json).and_then(|_| writer.flush()) {
                    eprintln!("Failed to write to stdout/writer: {}", e);
                    executor.clean_up();
                    return Err(ExecutorError::Io(e));
                }
            }
            Err(e) => {
                eprintln!("Error reading input: {}", e);
                executor.clean_up();
                return Err(e);
            }
        }
    }
    Ok(())
}

/// Runs the executor message loop with bounded non-blocking stdin channel, periodic child liveness checks,
/// and responsive signal shutdown.
pub fn run_executor_stdin_stdout(
    mut executor: ArmaExecutor,
    shutdown_flag: Arc<AtomicBool>,
) -> Result<(), ExecutorError> {
    // Channel bounded to 32 items to avoid unbounded memory growth
    let (tx, rx) = sync_channel::<Option<Result<String, ExecutorError>>>(32);

    thread::spawn(move || {
        let stdin = std::io::stdin();
        let mut handle = stdin.lock();
        let mut line = String::new();
        loop {
            line.clear();
            match read_bounded_line(&mut handle, &mut line, MAX_LINE_BYTES) {
                Ok(None) => {
                    let _ = tx.send(None);
                    break;
                }
                Ok(Some(_)) => {
                    if tx.send(Some(Ok(line.clone()))).is_err() {
                        break;
                    }
                }
                Err(e) => {
                    let _ = tx.send(Some(Err(e)));
                }
            }
        }
    });

    let stdout = std::io::stdout();
    let mut out_handle = stdout.lock();

    loop {
        if shutdown_flag.load(Ordering::SeqCst) {
            eprintln!("Shutdown signal detected, cleaning up...");
            executor.clean_up();
            break;
        }

        match rx.recv_timeout(Duration::from_millis(50)) {
            Ok(Some(Ok(line))) => {
                let resp = executor.process_raw_request(&line);
                let resp_json =
                    serde_json::to_string(&resp).map_err(crate::error::ConfigError::from)?;
                if let Err(e) =
                    writeln!(out_handle, "{}", resp_json).and_then(|_| out_handle.flush())
                {
                    eprintln!("Failed to write to stdout / broken pipe: {}", e);
                    executor.clean_up();
                    return Err(ExecutorError::Io(e));
                }

                if executor.is_shut_down() {
                    eprintln!("Shutdown request executed, exiting process...");
                    break;
                }
            }
            Ok(Some(Err(ExecutorError::InputLineTooLong(msg)))) => {
                let resp = ProtocolResponse::error("unknown".to_string(), "INVALID_REQUEST", msg);
                let resp_json =
                    serde_json::to_string(&resp).map_err(crate::error::ConfigError::from)?;
                if let Err(e) =
                    writeln!(out_handle, "{}", resp_json).and_then(|_| out_handle.flush())
                {
                    eprintln!("Failed to write to stdout / broken pipe: {}", e);
                    executor.clean_up();
                    return Err(ExecutorError::Io(e));
                }
            }
            Ok(Some(Err(e))) => {
                eprintln!("Error reading input: {}", e);
                executor.clean_up();
                return Err(e);
            }
            Ok(None) => {
                // EOF
                eprintln!("stdin reached EOF, cleaning up...");
                executor.clean_up();
                break;
            }
            Err(RecvTimeoutError::Timeout) => {
                // Periodic child liveness verification
                executor.check_child_liveness();
            }
            Err(RecvTimeoutError::Disconnected) => {
                eprintln!("stdin reader thread disconnected, cleaning up...");
                executor.clean_up();
                break;
            }
        }
    }

    Ok(())
}
