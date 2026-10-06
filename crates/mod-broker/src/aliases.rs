//! Tool-name aliases between the Claude Code names a mod sees in `tool.call`
//! payloads and matchers, and Ginno's own builtin tool names (design §15.8).
//! Unmapped names pass through unchanged, both ways.

use std::collections::HashMap;

/// Claude Code tool name → Ginno builtin tool name for the tools both ship.
/// The P1 entries (`AskUserQuestion`, `Skill`, `Task`, `TodoWrite`) are listed
/// now so matchers written against them behave once those tools exist.
fn default_aliases() -> HashMap<&'static str, &'static str> {
    HashMap::from([
        ("Read", "read_file"),
        ("Write", "write_file"),
        ("Edit", "edit_file"),
        ("Bash", "bash"),
        ("Glob", "glob"),
        ("Grep", "grep"),
        ("AskUserQuestion", "ask"),
        ("Skill", "skill"),
        ("Task", "subagent"),
        ("TodoWrite", "todo"),
    ])
}

/// Two-way tool-name translation. Built once per broker; cheap to clone.
#[derive(Debug, Clone, Default)]
pub struct ToolNameAliases {
    forward: HashMap<String, String>,
    reverse: HashMap<String, String>,
}

impl ToolNameAliases {
    pub fn new() -> Self {
        let forward: HashMap<String, String> =
            default_aliases().into_iter().map(|(k, v)| (k.to_string(), v.to_string())).collect();
        let reverse: HashMap<String, String> =
            forward.iter().map(|(mod_name, host)| (host.clone(), mod_name.clone())).collect();
        Self { forward, reverse }
    }

    /// The name a mod sees for a Ginno tool: its Claude Code alias, or the name itself.
    pub fn to_mod(&self, host_name: &str) -> String {
        self.reverse.get(host_name).cloned().unwrap_or_else(|| host_name.to_string())
    }

    /// The Ginno tool a mod named: the alias target, or the name itself.
    pub fn to_host(&self, mod_name: &str) -> String {
        self.forward.get(mod_name).cloned().unwrap_or_else(|| mod_name.to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn translates_both_ways_and_passes_unknown_through() {
        let aliases = ToolNameAliases::new();
        assert_eq!(aliases.to_host("Read"), "read_file");
        assert_eq!(aliases.to_host("Bash"), "bash");
        assert_eq!(aliases.to_host("Custom"), "Custom");
        assert_eq!(aliases.to_mod("read_file"), "Read");
        assert_eq!(aliases.to_mod("bash"), "Bash");
        assert_eq!(aliases.to_mod("unknown_tool"), "unknown_tool");
    }
}
