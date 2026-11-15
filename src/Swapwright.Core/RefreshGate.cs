namespace Swapwright.Core;

/// <summary>
/// Refresh state machine: 0 idle, 1 running, 2 running with one more run queued. A normal request while running is
/// dropped; a forced one queues exactly one extra run. All transitions are single compare-exchanges, so a queued run
/// can never be wiped by the loop finishing.
/// </summary>
public sealed class RefreshGate
{
    private int _state;

    public bool IsBusy => Volatile.Read(ref _state) != 0;

    /// <summary>True when the caller must start the refresh loop (state went 0 -> 1).</summary>
    public bool Request(bool force)
    {
        while (true)
        {
            var state = Volatile.Read(ref _state);
            if (state == 0)
            {
                if (Interlocked.CompareExchange(ref _state, 1, 0) != 0) continue;
                return true;
            }
            if (!force || state == 2) return false;
            if (Interlocked.CompareExchange(ref _state, 2, 1) == 1) return false;
        }
    }

    /// <summary>
    /// Called by the loop after each run. True = a run was queued, run again (state stays 1). False = now idle.
    /// <paramref name="stop"/> drops a queued run and goes idle (disposal).
    /// </summary>
    public bool Finish(bool stop = false)
    {
        if (stop)
        {
            Volatile.Write(ref _state, 0);
            return false;
        }
        while (true)
        {
            if (Interlocked.CompareExchange(ref _state, 1, 2) == 2) return true;
            if (Interlocked.CompareExchange(ref _state, 0, 1) == 1) return false;
        }
    }
}
