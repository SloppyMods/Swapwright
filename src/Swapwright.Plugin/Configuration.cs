using Dalamud.Configuration;

namespace Swapwright.Plugin;

/// <summary>Dalamud's own plugin config (the only file this plugin writes).</summary>
[Serializable]
public sealed class Configuration : IPluginConfiguration
{
    public int Version { get; set; } = 1;

    public bool ShowOnlyUsed { get; set; }

    /// <summary>Hide face-only emotes (the game's Expressions) from the emote list; dance mods can't go on them.</summary>
    public bool HideFacial { get; set; } = true;

    /// <summary>Folder (dir) of the last mod picked in the mod combo.</summary>
    public string? LastModDir { get; set; }

    public int SwapdPort { get; set; } = 47815;
}
