namespace Swapwright.Core;

/// <summary>One TrySetMod call: set <see cref="Dir"/> enabled or disabled in the player's collection.</summary>
public readonly record struct ModToggle(string Dir, bool Enable);

/// <summary>
/// Decision D3: the emote checkbox is on when at least one mod using the emote is enabled.
/// Turning it on enables exactly one of those mods and disables the others so they never fight.
/// Turning it off disables all of them.
/// </summary>
public static class TogglePlanner
{
    public static bool IsChecked(IEnumerable<ModEntry> users, Func<string, bool> isEnabled)
        => users.Any(u => isEnabled(u.Dir));

    /// <summary>The user has to pick when more than one mod uses the emote.</summary>
    public static bool NeedsChoice(IReadOnlyList<ModEntry> users) => users.Count > 1;

    /// <summary>
    /// The default mod to enable: a swap copy marked "worked", else the most recent entry.
    /// swapd gives no timestamps, so "most recent" is the last entry in /used order, preferring
    /// swap copies (awaiting = freshly installed) over non-swap mods.
    /// </summary>
    public static ModEntry? DefaultChoice(IReadOnlyList<ModEntry> users)
    {
        if (users.Count == 0) return null;
        var worked = users.LastOrDefault(u => u.IsSwap && u.Status == Outcome.Worked);
        if (worked is not null) return worked;
        var awaiting = users.LastOrDefault(u => u.IsSwap && u.Status == "awaiting");
        if (awaiting is not null) return awaiting;
        var swap = users.LastOrDefault(u => u.IsSwap);
        return swap ?? users[^1];
    }

    /// <summary>Toggles for turning the emote on with <paramref name="chosenDir"/> (null = <see cref="DefaultChoice"/>).</summary>
    public static IReadOnlyList<ModToggle> PlanOn(IReadOnlyList<ModEntry> users, string? chosenDir = null)
    {
        var chosen = chosenDir is not null
            ? users.FirstOrDefault(u => u.Dir == chosenDir)
            : DefaultChoice(users);
        if (chosen is null) return Array.Empty<ModToggle>();

        var plan = new List<ModToggle> { new(chosen.Dir, true) };
        foreach (var dir in users.Select(u => u.Dir).Distinct())
            if (dir != chosen.Dir) plan.Add(new ModToggle(dir, false));
        return plan;
    }

    public static IReadOnlyList<ModToggle> PlanOff(IReadOnlyList<ModEntry> users)
        => users.Select(u => u.Dir).Distinct().Select(d => new ModToggle(d, false)).ToList();

    /// <summary>
    /// After Install: enable the new folder and disable every other mod using the target emote (same rule as
    /// <see cref="PlanOn"/>), plus the original the swap was built from, which uses the donor emote instead.
    /// </summary>
    public static IReadOnlyList<ModToggle> PlanInstall(IReadOnlyList<ModEntry> targetUsers, string newDir, string originalDir)
    {
        var users = targetUsers.Where(u => u.Dir != newDir).Append(new ModEntry { Dir = newDir, Name = newDir, IsSwap = true }).ToList();
        var plan = PlanOn(users, newDir).ToList();
        if (originalDir != newDir && plan.All(t => t.Dir != originalDir))
            plan.Add(new ModToggle(originalDir, false));
        return plan;
    }
}
