namespace Swapwright.Core;

/// <summary>
/// Maps swapd's PAP keys ("emote/dance16_loop") to game emotes. An emote's keys are the Keys of its
/// ActionTimeline[0] and [1] rows. A mod uses an emote if any of its keys equals one of the emote's keys.
/// </summary>
public static class EmoteMatcher
{
    private const string FacialPrefix = "facial/";

    /// <summary>Normalises the raw ActionTimeline keys of one emote: trims, drops empty ones and duplicates.</summary>
    public static IReadOnlyList<string> EmoteKeys(IEnumerable<string?> rawKeys)
    {
        var result = new List<string>();
        foreach (var raw in rawKeys)
        {
            var key = Normalise(raw);
            if (key.Length == 0 || result.Contains(key, StringComparer.Ordinal)) continue;
            result.Add(key);
        }
        return result;
    }

    /// <summary>True when the emote only animates the face (every key under "facial/"), so a dance can't go on it.</summary>
    public static bool IsFacialOnly(IReadOnlyCollection<string> emoteKeys)
        => emoteKeys.Count > 0 && emoteKeys.All(k => k.StartsWith(FacialPrefix, StringComparison.Ordinal));

    /// <summary>Names of the game emotes that use this PAP key, joined with ", "; null when no emote does (the donor slot is unused in game).</summary>
    public static string? DanceLabel(string key, IEnumerable<(string Name, IReadOnlyCollection<string> Keys)> emotes)
    {
        var names = emotes.Where(e => e.Keys.Contains(key, StringComparer.Ordinal)).Select(e => e.Name).Distinct().ToList();
        return names.Count == 0 ? null : string.Join(", ", names);
    }

    public static bool Uses(ModEntry mod, IReadOnlyCollection<string> emoteKeys)
    {
        if (emoteKeys.Count == 0) return false;
        foreach (var k in mod.Keys)
        {
            var key = Normalise(k);
            if (key.Length > 0 && emoteKeys.Contains(key, StringComparer.Ordinal)) return true;
        }
        return false;
    }

    /// <summary>Every /used entry that uses the emote, in /used order.</summary>
    public static IReadOnlyList<ModEntry> ModsUsing(IEnumerable<ModEntry> used, IReadOnlyCollection<string> emoteKeys)
        => used.Where(m => Uses(m, emoteKeys)).ToList();

    private static string Normalise(string? key) => (key ?? string.Empty).Trim().Replace('\\', '/');
}
