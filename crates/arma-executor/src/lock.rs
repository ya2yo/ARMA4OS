use crate::error::LockError;
use std::fs::OpenOptions;
use std::io::Write;
use std::os::unix::io::AsRawFd;
use std::path::{Path, PathBuf};

#[derive(Debug)]
pub struct StateLock {
    path: PathBuf,
    file: Option<std::fs::File>,
}

impl StateLock {
    pub fn acquire<P: AsRef<Path>>(state_dir: P) -> Result<Self, LockError> {
        let dir = state_dir.as_ref();
        std::fs::create_dir_all(dir)
            .map_err(|e| LockError::Io(format!("Failed to create state_dir {:?}: {}", dir, e)))?;

        let lock_path = dir.join("executor.lock");
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(&lock_path)
            .map_err(|e| {
                LockError::Io(format!("Failed to open lock file {:?}: {}", lock_path, e))
            })?;

        let fd = file.as_raw_fd();
        let ret = unsafe { libc::flock(fd, libc::LOCK_EX | libc::LOCK_NB) };
        if ret != 0 {
            let err = std::io::Error::last_os_error();
            return Err(LockError::AlreadyLocked(format!(
                "Failed to acquire exclusive lock on {:?}: {}",
                lock_path, err
            )));
        }

        // Write executor's PID into lock file for tracking
        if let Ok(mut clone) = file.try_clone() {
            let _ = clone.set_len(0);
            let _ = writeln!(clone, "{}", std::process::id());
        }

        Ok(Self {
            path: lock_path,
            file: Some(file),
        })
    }

    pub fn lock_path(&self) -> &Path {
        &self.path
    }

    pub fn release(&mut self) {
        if let Some(file) = self.file.take() {
            let fd = file.as_raw_fd();
            unsafe {
                libc::flock(fd, libc::LOCK_UN);
            }
        }
    }
}

impl Drop for StateLock {
    fn drop(&mut self) {
        self.release();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_lock_acquire_and_exclusivity() {
        let temp_dir = tempfile::tempdir().unwrap();
        let lock1 = StateLock::acquire(temp_dir.path()).unwrap();
        assert!(lock1.lock_path().exists());

        // Second lock in same directory must fail
        let lock2 = StateLock::acquire(temp_dir.path());
        assert!(lock2.is_err());
        match lock2.unwrap_err() {
            LockError::AlreadyLocked(_) => {}
            other => panic!("Expected AlreadyLocked, got {:?}", other),
        }

        // Release first lock
        drop(lock1);

        // Third lock should now succeed
        let lock3 = StateLock::acquire(temp_dir.path());
        assert!(lock3.is_ok());
    }
}
