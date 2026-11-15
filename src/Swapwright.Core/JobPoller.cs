namespace Swapwright.Core;

/// <summary>Polls GET /job/&lt;id&gt; until the job leaves "running". Runs on a background task, never the UI thread.</summary>
public static class JobPoller
{
    /// <param name="get">One GET /job call.</param>
    /// <param name="onUpdate">Called with every status seen (including the final one).</param>
    /// <param name="maxErrors">Consecutive transport errors tolerated before giving up.</param>
    public static async Task<SwapdResult<JobStatus>> WaitAsync(
        Func<CancellationToken, Task<SwapdResult<JobStatus>>> get,
        Action<JobStatus> onUpdate,
        TimeSpan interval,
        CancellationToken ct,
        int maxErrors = 5)
    {
        var errors = 0;
        while (true)
        {
            ct.ThrowIfCancellationRequested();
            var r = await get(ct).ConfigureAwait(false);
            if (!r.Ok)
            {
                // 4xx (e.g. unknown job) will not fix itself; transport errors may.
                if (r.StatusCode is >= 400 and < 500 || ++errors >= maxErrors) return r;
            }
            else
            {
                errors = 0;
                onUpdate(r.Value!);
                if (!r.Value!.IsRunning) return r;
            }
            await Task.Delay(interval, ct).ConfigureAwait(false);
        }
    }
}
