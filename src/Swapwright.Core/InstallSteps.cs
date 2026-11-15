namespace Swapwright.Core;

/// <summary>What happened when an install plan was applied. <see cref="DisableSkipped"/> = the enable failed, so nothing was disabled.</summary>
public sealed record InstallApplyResult(bool Enabled, string EnableDetail, IReadOnlyList<string> Disabled, IReadOnlyList<string> Failed, bool DisableSkipped);

public static class InstallSteps
{
    /// <summary>
    /// Applies a <see cref="TogglePlanner.PlanInstall"/> plan: every enable first; if an enable fails, no disable is
    /// attempted (otherwise the user ends up with the emote off everywhere). <paramref name="set"/> returns
    /// (ok, detail); ok = Success or NothingChanged.
    /// </summary>
    public static InstallApplyResult Apply(IReadOnlyList<ModToggle> plan, Func<ModToggle, (bool Ok, string Detail)> set)
    {
        var enabled = true;
        var enableDetail = "n/a";
        foreach (var t in plan.Where(t => t.Enable))
        {
            var (ok, detail) = set(t);
            enableDetail = detail;
            if (!ok) enabled = false;
        }
        var disabled = new List<string>();
        var failed = new List<string>();
        if (!enabled) return new InstallApplyResult(false, enableDetail, disabled, failed, plan.Any(t => !t.Enable));
        foreach (var t in plan.Where(t => !t.Enable))
        {
            var (ok, detail) = set(t);
            if (ok) disabled.Add(t.Dir); else failed.Add($"{t.Dir}: {detail}");
        }
        return new InstallApplyResult(true, enableDetail, disabled, failed, false);
    }
}
