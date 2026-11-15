using Swapwright.Core;
using Xunit;

namespace Swapwright.Tests;

public class InstallStepsTests
{
    private static ModEntry M(string dir) => new() { Dir = dir, Name = dir };

    [Fact]
    public void Apply_EnablesFirstThenDisablesOthers()
    {
        var plan = TogglePlanner.PlanInstall(new[] { M("a"), M("b") }, "new", "orig");
        var calls = new List<ModToggle>();
        var r = InstallSteps.Apply(plan, t => { calls.Add(t); return (true, "Success"); });
        Assert.True(r.Enabled);
        Assert.Equal(new ModToggle("new", true), calls[0]);
        Assert.Equal(new[] { "a", "b", "orig" }.OrderBy(x => x), r.Disabled.OrderBy(x => x));
        Assert.Empty(r.Failed);
        Assert.False(r.DisableSkipped);
    }

    [Fact]
    public void Apply_FailedEnable_DisablesNothing()
    {
        var plan = TogglePlanner.PlanInstall(new[] { M("a") }, "new", "orig");
        var calls = new List<ModToggle>();
        var r = InstallSteps.Apply(plan, t => { calls.Add(t); return (false, "ModMissing"); });
        Assert.False(r.Enabled);
        Assert.Equal("ModMissing", r.EnableDetail);
        Assert.Single(calls);
        Assert.True(calls[0].Enable);
        Assert.Empty(r.Disabled);
        Assert.True(r.DisableSkipped);
    }

    [Fact]
    public void Apply_ReportsFailedDisables()
    {
        var plan = TogglePlanner.PlanInstall(new[] { M("a") }, "new", "orig");
        var r = InstallSteps.Apply(plan, t => t.Dir == "a" ? (false, "Err") : (true, "Success"));
        Assert.True(r.Enabled);
        Assert.Equal(new[] { "a: Err" }, r.Failed);
        Assert.Equal(new[] { "orig" }, r.Disabled);
    }
}

public class RefreshGateTests
{
    [Fact]
    public void Idle_RequestStartsLoop_SecondPlainRequestDropped()
    {
        var g = new RefreshGate();
        Assert.True(g.Request(false));
        Assert.True(g.IsBusy);
        Assert.False(g.Request(false));
        Assert.False(g.Finish());
        Assert.False(g.IsBusy);
    }

    [Fact]
    public void ForceWhileRunning_QueuesExactlyOneMoreRun()
    {
        var g = new RefreshGate();
        Assert.True(g.Request(false));
        Assert.False(g.Request(true));
        Assert.False(g.Request(true));
        Assert.True(g.Finish());   // queued run
        Assert.False(g.Finish());  // nothing more
        Assert.False(g.IsBusy);
    }

    [Fact]
    public void ForceDuringQueuedRun_IsNotLost()
    {
        var g = new RefreshGate();
        g.Request(false);
        g.Request(true);
        Assert.True(g.Finish());
        Assert.False(g.Request(true)); // arrives while the queued run executes
        Assert.True(g.Finish());
        Assert.False(g.Finish());
    }

    [Fact]
    public void Stop_DropsQueuedRunAndGoesIdle()
    {
        var g = new RefreshGate();
        g.Request(false);
        g.Request(true);
        Assert.False(g.Finish(stop: true));
        Assert.False(g.IsBusy);
    }

    [Fact]
    public async Task Concurrent_ForcedRequests_AreAlwaysFollowedByAFreshRun()
    {
        for (var iter = 0; iter < 300; iter++)
        {
            var g = new RefreshGate();
            var runs = 0;
            var loops = new System.Collections.Concurrent.ConcurrentBag<Task>();
            void Loop()
            {
                do Interlocked.Increment(ref runs); // a run "starts" here
                while (g.Finish());
            }
            void Req(bool force, List<int> before)
            {
                var snap = Volatile.Read(ref runs); // runs started before this request
                if (g.Request(force)) loops.Add(Task.Run(Loop));
                lock (before) before.Add(snap);
            }
            var snaps = new List<int>();
            Req(false, snaps);
            await Task.WhenAll(Enumerable.Range(0, 4).Select(_ => Task.Run(() => Req(true, snaps))));
            await Task.WhenAll(loops);
            Assert.False(g.IsBusy);
            // every forced request must have a run that started after it was made
            Assert.True(runs > snaps.Skip(1).Max() || snaps.Skip(1).Max() == 0 && runs >= 1);
        }
    }
}
