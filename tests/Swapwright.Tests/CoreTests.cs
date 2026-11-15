using Swapwright.Core;
using Xunit;

namespace Swapwright.Tests;

public class EmoteMatcherTests
{
    private static ModEntry Mod(string dir, params string[] keys) => new() { Dir = dir, Name = dir, Keys = keys.ToList() };

    [Fact]
    public void EmoteKeys_SkipsEmptyAndDuplicates()
    {
        var keys = EmoteMatcher.EmoteKeys(new[] { "emote/dance16_loop", "", null, "  ", "emote/dance16_loop", "emote/dance16_start" });
        Assert.Equal(new[] { "emote/dance16_loop", "emote/dance16_start" }, keys);
    }

    [Fact]
    public void IsFacialOnly_TrueWhenEveryKeyIsFacial()
    {
        Assert.True(EmoteMatcher.IsFacialOnly(EmoteMatcher.EmoteKeys(new[] { "facial/pose/smile", null })));
    }

    [Fact]
    public void IsFacialOnly_FalseForBodyOrMixedOrNoKeys()
    {
        Assert.False(EmoteMatcher.IsFacialOnly(EmoteMatcher.EmoteKeys(new[] { "emote/clap", "emote/s_clap" })));
        Assert.False(EmoteMatcher.IsFacialOnly(EmoteMatcher.EmoteKeys(new[] { "emote/clap", "facial/pose/smile" })));
        Assert.False(EmoteMatcher.IsFacialOnly(EmoteMatcher.EmoteKeys(new string?[] { null, "" })));
    }

    [Fact]
    public void DanceLabel_NamesEmotesUsingTheKey()
    {
        var emotes = new (string, IReadOnlyCollection<string>)[]
        {
            ("Cheer On", new[] { "emote_sp/sp10_loop", "emote_sp/sp10_start" }),
            ("Cheer Jump", new[] { "emote_sp/sp12_loop" }),
        };
        Assert.Equal("Cheer Jump", EmoteMatcher.DanceLabel("emote_sp/sp12_loop", emotes));
        Assert.Equal("Cheer On", EmoteMatcher.DanceLabel("emote_sp/sp10_loop", emotes));
        Assert.Null(EmoteMatcher.DanceLabel("emote/loop_emot19_loop", emotes));
    }

    [Fact]
    public void Uses_MatchesAnyKey()
    {
        var emote = EmoteMatcher.EmoteKeys(new[] { "emote/laugh", "emote/laugh_st" });
        Assert.True(EmoteMatcher.Uses(Mod("a", "emote/x", "emote/laugh_st"), emote));
        Assert.False(EmoteMatcher.Uses(Mod("b", "emote/laugh2"), emote));
    }

    [Fact]
    public void Uses_IsExactAndCaseSensitive()
    {
        var emote = EmoteMatcher.EmoteKeys(new[] { "emote/j_laugh" });
        Assert.False(EmoteMatcher.Uses(Mod("a", "emote/laugh"), emote));
        Assert.False(EmoteMatcher.Uses(Mod("a", "EMOTE/J_LAUGH"), emote));
        Assert.True(EmoteMatcher.Uses(Mod("a", " emote/j_laugh "), emote));
    }

    [Fact]
    public void Uses_EmoteWithoutKeys_NeverMatches()
    {
        Assert.False(EmoteMatcher.Uses(Mod("a", ""), EmoteMatcher.EmoteKeys(new string?[] { null, "" })));
    }

    [Fact]
    public void ModsUsing_KeepsUsedOrder()
    {
        var used = new[] { Mod("a", "emote/cackle"), Mod("b", "emote/pray"), Mod("c", "emote_sp/cackle", "emote/cackle") };
        var r = EmoteMatcher.ModsUsing(used, EmoteMatcher.EmoteKeys(new[] { "emote/cackle" }));
        Assert.Equal(new[] { "a", "c" }, r.Select(m => m.Dir));
    }
}

public class TogglePlannerTests
{
    private static ModEntry M(string dir, bool swap = false, string status = "unknown") =>
        new() { Dir = dir, Name = dir, IsSwap = swap, Status = status };

    [Fact]
    public void IsChecked_AnyEnabled()
    {
        var users = new[] { M("a"), M("b") };
        Assert.True(TogglePlanner.IsChecked(users, d => d == "b"));
        Assert.False(TogglePlanner.IsChecked(users, _ => false));
        Assert.False(TogglePlanner.IsChecked(Array.Empty<ModEntry>(), _ => true));
    }

    [Fact]
    public void DefaultChoice_PrefersWorkedSwap()
    {
        var users = new[] { M("orig"), M("s1", true, "worked"), M("s2", true, "awaiting"), M("s3", true, "failed") };
        Assert.Equal("s1", TogglePlanner.DefaultChoice(users)!.Dir);
    }

    [Fact]
    public void DefaultChoice_WithoutWorked_TakesMostRecentSwap()
    {
        var users = new[] { M("s1", true, "failed"), M("orig"), M("s2", true, "awaiting") };
        Assert.Equal("s2", TogglePlanner.DefaultChoice(users)!.Dir);
    }

    [Fact]
    public void DefaultChoice_NoSwaps_TakesLast()
    {
        Assert.Equal("b", TogglePlanner.DefaultChoice(new[] { M("a"), M("b") })!.Dir);
        Assert.Null(TogglePlanner.DefaultChoice(Array.Empty<ModEntry>()));
    }

    [Fact]
    public void PlanOn_EnablesChosen_DisablesOthers()
    {
        var users = new[] { M("a"), M("b", true, "worked"), M("c") };
        var plan = TogglePlanner.PlanOn(users, "c");
        Assert.Equal(new ModToggle("c", true), plan[0]);
        Assert.Contains(new ModToggle("a", false), plan);
        Assert.Contains(new ModToggle("b", false), plan);
        Assert.Equal(3, plan.Count);
    }

    [Fact]
    public void PlanOn_DefaultsWhenNoChoice()
    {
        var plan = TogglePlanner.PlanOn(new[] { M("a"), M("b", true, "worked") });
        Assert.Equal(new ModToggle("b", true), plan[0]);
        Assert.Equal(new ModToggle("a", false), plan[1]);
    }

    [Fact]
    public void PlanOn_UnknownChoice_DoesNothing()
    {
        Assert.Empty(TogglePlanner.PlanOn(new[] { M("a") }, "zzz"));
        Assert.Empty(TogglePlanner.PlanOn(Array.Empty<ModEntry>()));
    }

    [Fact]
    public void PlanOn_SingleUser_OnlyEnables()
    {
        Assert.Equal(new[] { new ModToggle("a", true) }, TogglePlanner.PlanOn(new[] { M("a") }));
    }

    [Fact]
    public void PlanOff_DisablesAll()
    {
        Assert.Equal(new[] { new ModToggle("a", false), new ModToggle("b", false) },
            TogglePlanner.PlanOff(new[] { M("a"), M("b"), M("a") }));
    }

    [Fact]
    public void NeedsChoice_OnlyForSeveral()
    {
        Assert.False(TogglePlanner.NeedsChoice(new[] { M("a") }));
        Assert.True(TogglePlanner.NeedsChoice(new[] { M("a"), M("b") }));
    }

    [Fact]
    public void PlanInstall_EnablesNew_DisablesAllOtherTargetUsersAndOriginal()
    {
        var target = new[] { M("other swap", true, "worked"), M("vanilla-ish"), M("other swap") };
        var plan = TogglePlanner.PlanInstall(target, "new [Wave test]", "orig");
        Assert.Equal(new ModToggle("new [Wave test]", true), plan[0]);
        Assert.Contains(new ModToggle("other swap", false), plan);
        Assert.Contains(new ModToggle("vanilla-ish", false), plan);
        Assert.Contains(new ModToggle("orig", false), plan);
        Assert.Equal(4, plan.Count);
    }

    [Fact]
    public void PlanInstall_NewAlreadyListed_OriginalAlsoTargetUser_NoDuplicates()
    {
        var plan = TogglePlanner.PlanInstall(new[] { M("orig"), M("new", true, "awaiting") }, "new", "orig");
        Assert.Equal(new[] { new ModToggle("new", true), new ModToggle("orig", false) }, plan);
        Assert.Equal(new[] { new ModToggle("new", true), new ModToggle("orig", false) },
            TogglePlanner.PlanInstall(Array.Empty<ModEntry>(), "new", "orig"));
    }
}
