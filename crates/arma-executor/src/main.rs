use arma_executor::config::ExecutorConfig;
use arma_executor::executor::ArmaExecutor;
use arma_executor::lock::StateLock;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

static SHUTDOWN_FLAG: AtomicBool = AtomicBool::new(false);

extern "C" fn signal_handler(_sig: libc::c_int) {
    SHUTDOWN_FLAG.store(true, Ordering::SeqCst);
}

fn setup_signal_handlers() {
    unsafe {
        let mut sa: libc::sigaction = std::mem::zeroed();
        sa.sa_sigaction = signal_handler as *const () as usize;
        sa.sa_flags = 0;
        libc::sigemptyset(&mut sa.sa_mask);
        libc::sigaction(libc::SIGINT, &sa, std::ptr::null_mut());
        libc::sigaction(libc::SIGTERM, &sa, std::ptr::null_mut());
    }
}

fn print_usage() {
    eprintln!("Usage: arma-executor --config <path-to-config.json>");
    eprintln!();
    eprintln!("Options:");
    eprintln!("  -c, --config <PATH>   Path to the executor configuration JSON file");
    eprintln!("  -h, --help            Print help information");
}

fn parse_cli_config_path() -> Result<PathBuf, String> {
    let mut args = std::env::args().skip(1);
    let mut config_path = None;

    while let Some(arg) = args.next() {
        if arg == "-h" || arg == "--help" {
            print_usage();
            std::process::exit(0);
        } else if arg == "-c" || arg == "--config" {
            match args.next() {
                Some(p) => config_path = Some(PathBuf::from(p)),
                None => return Err("Missing argument for --config".to_string()),
            }
        } else if let Some(stripped) = arg.strip_prefix("--config=") {
            config_path = Some(PathBuf::from(stripped));
        } else {
            return Err(format!("Unknown argument: {}", arg));
        }
    }

    config_path.ok_or_else(|| "Missing required argument: --config <path>".to_string())
}

fn main() {
    let config_path = match parse_cli_config_path() {
        Ok(p) => p,
        Err(e) => {
            eprintln!("Error: {}", e);
            print_usage();
            std::process::exit(1);
        }
    };

    eprintln!("Loading configuration from {:?}", config_path);
    let config = match ExecutorConfig::load_from_file(&config_path) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("Failed to load configuration: {}", e);
            std::process::exit(1);
        }
    };

    eprintln!(
        "Acquiring state directory lock in {:?}...",
        config.state_dir
    );
    let _lock = match StateLock::acquire(&config.state_dir) {
        Ok(l) => l,
        Err(e) => {
            eprintln!("Exclusive lock failed: {}", e);
            std::process::exit(1);
        }
    };
    eprintln!("Exclusive lock acquired on {:?}", _lock.lock_path());

    setup_signal_handlers();

    let executor = ArmaExecutor::new(config);
    let shutdown_arc = Arc::new(AtomicBool::new(false));

    // Monitor global signal flag in a background watcher or check SHUTDOWN_FLAG directly
    let flag_clone = Arc::clone(&shutdown_arc);
    std::thread::spawn(move || {
        while !SHUTDOWN_FLAG.load(Ordering::SeqCst) {
            std::thread::sleep(std::time::Duration::from_millis(50));
        }
        flag_clone.store(true, Ordering::SeqCst);
    });

    eprintln!("arma-executor started, awaiting requests on stdin...");
    if let Err(e) = arma_executor::run_executor_stdin_stdout(executor, shutdown_arc) {
        eprintln!("Executor encountered fatal error: {}", e);
        std::process::exit(1);
    }

    eprintln!("arma-executor stopped gracefully.");
}
