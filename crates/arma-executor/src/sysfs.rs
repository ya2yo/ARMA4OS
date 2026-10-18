use std::path::{Path, PathBuf};

#[derive(Debug, Clone)]
pub struct SysfsReader {
    root: PathBuf,
}

impl SysfsReader {
    pub fn new<P: AsRef<Path>>(root: P) -> Self {
        Self {
            root: root.as_ref().to_path_buf(),
        }
    }

    pub fn root(&self) -> &Path {
        &self.root
    }

    pub fn is_sched_ext_supported(&self) -> bool {
        self.root.exists() && self.root.is_dir()
    }

    pub fn read_state(&self) -> Result<String, std::io::Error> {
        let state_path = self.root.join("state");
        if !state_path.exists() {
            return Ok("unsupported".to_string());
        }
        let content = std::fs::read_to_string(state_path)?;
        Ok(content.trim().to_string())
    }

    pub fn read_ops(&self) -> Result<Option<String>, std::io::Error> {
        // First try <sysfs_root>/root/ops, then fallback to <sysfs_root>/ops
        let root_ops = self.root.join("root").join("ops");
        let ops_path = if root_ops.exists() {
            root_ops
        } else {
            self.root.join("ops")
        };

        if !ops_path.exists() {
            return Ok(None);
        }

        let content = std::fs::read_to_string(ops_path)?;
        let trimmed = content.trim();
        if trimmed.is_empty() {
            Ok(None)
        } else {
            Ok(Some(trimmed.to_string()))
        }
    }

    pub fn read_state_and_ops(&self) -> Result<(String, Option<String>), std::io::Error> {
        let state = self.read_state()?;
        let ops = self.read_ops()?;
        Ok((state, ops))
    }
}
