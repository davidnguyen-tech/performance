// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.
// See the LICENSE file in the project root for more information.

using System;
using System.Collections;
using System.Collections.Generic;
using System.Collections.Immutable;
using System.IO;
using System.Linq;
using System.Threading.Tasks;
using BenchmarkDotNet.Attributes;
using BenchmarkDotNet.Configs;
using BenchmarkDotNet.ConsoleArguments;
using BenchmarkDotNet.Extensions;
using BenchmarkDotNet.Jobs;
using BenchmarkDotNet.Loggers;
using BenchmarkDotNet.Running;
using BenchmarkDotNet.Toolchains.CsProj;
using BenchmarkDotNet.Validators;
using MicroBenchmarks;
using Reporting;
using Xunit;

namespace Tests
{
    public class RuntimePackageConfigTests
    {
        private const string PackageVersion = "12.0.0-gcbase.1.26465.1";

        [Fact]
        public void NoPackageVariablesPreservesNormalMode()
            => Assert.Null(RuntimePackageConfig.FromEnvironment(new TestEnvironment(), ".NETCoreApp,Version=v11.0"));

        [Fact]
        public void SmokeModeUsesTheRepositoryOwnedDryJob()
        {
            RuntimePackageConfig? package = RuntimePackageConfig.FromEnvironment(
                new TestEnvironment(PackageVersion, "linux-x64", "1"),
                ".NETCoreApp,Version=v11.0");
            IConfig config = RecommendedConfig.Create(
                new DirectoryInfo(Path.GetTempPath()),
                ImmutableHashSet.Create("Runtime"),
                runtimePackage: package);

            Job job = Assert.Single(config.GetJobs());
            Assert.Equal(1, job.Run.LaunchCount);
            Assert.Equal(0, job.Run.WarmupCount);
            Assert.Equal(1, job.Run.IterationCount);
        }

        [Theory]
        [InlineData("")]
        [InlineData("0")]
        [InlineData("1")]
        public void AcceptsSupportedSmokeValues(string value)
            => Assert.NotNull(RuntimePackageConfig.FromEnvironment(
                new TestEnvironment(PackageVersion, "linux-x64", value),
                ".NETCoreApp,Version=v11.0"));

        [Theory]
        [InlineData("true")]
        [InlineData("yes")]
        [InlineData("2")]
        public void RejectsUnsupportedSmokeValues(string value)
            => Assert.Throws<ArgumentException>(() => RuntimePackageConfig.FromEnvironment(
                new TestEnvironment(PackageVersion, "linux-x64", value),
                ".NETCoreApp,Version=v11.0"));

        [Theory]
        [InlineData(null)]
        [InlineData("")]
        [InlineData("*")]
        [InlineData("12.0.*-gcbase")]
        [InlineData("[12.0.0,13.0.0)")]
        [InlineData("12.0.0-gcbase /p:SelfContained=false")]
        [InlineData("12.0.0-gcbase+metadata")]
        public void RejectsNonExactVersions(string? version)
            => Assert.Throws<ArgumentException>(() => new RuntimePackageConfig(version, "linux-x64", "net11.0"));

        [Theory]
        [InlineData(null)]
        [InlineData("")]
        [InlineData("linux-x64;win-x64")]
        [InlineData("../linux-x64")]
        public void RejectsInvalidRuntimeIdentifiers(string? rid)
            => Assert.Throws<ArgumentException>(() => new RuntimePackageConfig(PackageVersion, rid, "net11.0"));

        [Theory]
        [InlineData(PackageVersion, null)]
        [InlineData(null, "linux-x64")]
        public void RejectsPartialConfiguration(string? version, string? rid)
        {
            var environment = new TestEnvironment(version, rid);
            Assert.Throws<ArgumentException>(() => RuntimePackageConfig.FromEnvironment(environment, ".NETCoreApp,Version=v11.0"));
        }

        [Fact]
        public void UsesApplicationTargetFramework()
        {
            RuntimePackageConfig? package = RuntimePackageConfig.FromEnvironment(
                new TestEnvironment(PackageVersion, "linux-x64"), ".NETCoreApp,Version=v11.0");

            Assert.NotNull(package);
            Assert.Equal("net11.0", package.TargetFrameworkMoniker);
        }

        [Fact]
        public void RejectsNonCoreApplication()
            => Assert.Throws<ArgumentException>(() => RuntimePackageConfig.FromEnvironment(
                new TestEnvironment(PackageVersion, "linux-x64"), ".NETFramework,Version=v4.8"));

        [Fact]
        public void ConfiguresGeneratedProjectWithoutReplacingMeasurementSettings()
        {
            var package = new RuntimePackageConfig(PackageVersion, "linux-x64", "net11.0");
            Job job = package.ApplyTo(Job.Default.WithWarmupCount(3));

            var toolchain = Assert.IsType<CsProjCoreToolchain>(job.Infrastructure.Toolchain);
            var generator = Assert.IsType<CsProjGenerator>(toolchain.Generator);
            Assert.Equal(PackageVersion, generator.RuntimeFrameworkVersion);
            Assert.Equal("net11.0", generator.TargetFrameworkMoniker);
            Assert.Equal(3, job.Run.WarmupCount);
            Assert.False(job.Environment.Gc.Server);
            Assert.True(job.Environment.Gc.Concurrent);
            Assert.NotNull(job.Infrastructure.Arguments);
            Assert.Equal(new[]
            {
                $"/p:RuntimeFrameworkVersion={PackageVersion}",
                "/p:RuntimeIdentifier=linux-x64",
                "/p:SelfContained=true",
                "/p:UseAppHost=true"
            }, job.Infrastructure.Arguments.Select(argument => argument.TextRepresentation));
        }

        [Fact]
        public void RecommendedConfigKeepsOneJob()
        {
            var package = new RuntimePackageConfig(PackageVersion, "linux-x64", "net11.0");
            IConfig config = RecommendedConfig.Create(
                new DirectoryInfo(Path.GetTempPath()),
                ImmutableHashSet.Create("Runtime"),
                runtimePackage: package);

            Job job = Assert.Single(config.GetJobs());
            Assert.Contains(PackageVersion, job.Id);
            Assert.Contains(package, config.GetValidators());
        }

        [Fact]
        public void PackageJobPassesValidation()
        {
            var package = new RuntimePackageConfig(PackageVersion, "linux-x64", "net11.0");
            IConfig config = ManualConfig.CreateEmpty().AddJob(package.ApplyTo(Job.Default));
            BenchmarkRunInfo benchmarks = BenchmarkConverter.TypeToBenchmarks(typeof(Probe), config);
            var parameters = new ValidationParameters(benchmarks.BenchmarksCases, benchmarks.Config);

            Assert.Empty(package.ValidateAsync(parameters).ToBlockingEnumerable());
        }

        [Fact]
        public void RuntimeComparisonIsRejected()
        {
            var package = new RuntimePackageConfig(PackageVersion, "linux-x64", "net11.0");
            IConfig config = ManualConfig.CreateEmpty()
                .AddJob(package.ApplyTo(Job.Default))
                .AddJob(Job.Default.WithId("InstalledRuntime"));
            BenchmarkRunInfo benchmarks = BenchmarkConverter.TypeToBenchmarks(typeof(Probe), config);

            Assert.NotEmpty(package.ValidateAsync(
                new ValidationParameters(benchmarks.BenchmarksCases, benchmarks.Config)).ToBlockingEnumerable());
        }

        [Fact]
        public void RuntimePropertyReplacementIsRejected()
        {
            var package = new RuntimePackageConfig(PackageVersion, "linux-x64", "net11.0");
            Job job = package.ApplyTo(Job.Default).WithArguments(
                new Argument[] { new MsBuildProperty("SelfContained", "false") });
            BenchmarkRunInfo benchmarks = BenchmarkConverter.TypeToBenchmarks(
                typeof(Probe), ManualConfig.CreateEmpty().AddJob(job));

            Assert.NotEmpty(package.ValidateAsync(
                new ValidationParameters(benchmarks.BenchmarksCases, benchmarks.Config)).ToBlockingEnumerable());
        }

        [Fact]
        public void GcModeReplacementIsRejected()
        {
            var package = new RuntimePackageConfig(PackageVersion, "linux-x64", "net11.0");
            Job job = package.ApplyTo(Job.Default).WithGcServer(true);
            BenchmarkRunInfo benchmarks = BenchmarkConverter.TypeToBenchmarks(
                typeof(Probe), ManualConfig.CreateEmpty().AddJob(job));

            Assert.NotEmpty(package.ValidateAsync(
                new ValidationParameters(benchmarks.BenchmarksCases, benchmarks.Config)).ToBlockingEnumerable());
        }

        [Fact]
        public void CoreRunOverrideIsRejected()
        {
            string coreRun = Path.GetTempFileName();
            try
            {
                var package = new RuntimePackageConfig(PackageVersion, "linux-x64", "net11.0");
                IConfig config = ManualConfig.CreateEmpty().AddJob(package.ApplyTo(Job.Default).AsDefault());
                (bool success, IConfig? parsed, _) = ConfigParser.Parse(
                    new[] { "--coreRun", coreRun }, ConsoleLogger.Default, config);
                Assert.True(success);
                Assert.NotNull(parsed);
                BenchmarkRunInfo benchmarks = BenchmarkConverter.TypeToBenchmarks(typeof(Probe), parsed);

                Assert.NotEmpty(package.ValidateAsync(
                    new ValidationParameters(benchmarks.BenchmarksCases, benchmarks.Config)).ToBlockingEnumerable());
            }
            finally
            {
                File.Delete(coreRun);
            }
        }

        [Fact]
        public void PilotFilterSelectsExactlyFourExistingAllocationCases()
        {
            var package = new RuntimePackageConfig(PackageVersion, "linux-x64", "net10.0");
            IConfig config = RecommendedConfig.Create(
                new DirectoryInfo(Path.GetTempPath()), ImmutableHashSet.Create(Categories.Runtime),
                runtimePackage: package);
            (bool success, IConfig? parsed, _) = ConfigParser.Parse(
                new[] { "--filter", "System.Tests.Perf_GC*NewOperator_Array*" }, ConsoleLogger.Default, config);
            Assert.True(success);
            Assert.NotNull(parsed);

            BenchmarkCase[] cases = new[] { typeof(System.Tests.Perf_GC<byte>), typeof(System.Tests.Perf_GC<char>) }
                .SelectMany(type => BenchmarkConverter.TypeToBenchmarks(type, parsed).BenchmarksCases)
                .Where(benchmark => parsed.GetFilters().All(filter => filter.Predicate(benchmark)))
                .ToArray();

            Assert.Equal(4, cases.Length);
            Assert.All(cases, benchmark => Assert.Equal("NewOperator_Array", benchmark.Descriptor.WorkloadMethod.Name));
        }

        [Fact]
        public void NormalCliOptionsPreservePackageSelection()
        {
            var package = new RuntimePackageConfig(PackageVersion, "linux-x64", "net11.0");
            IConfig config = ManualConfig.CreateEmpty().AddJob(package.ApplyTo(Job.Default).AsDefault());
            (bool success, IConfig? parsed, _) = ConfigParser.Parse(
                new[] { "--filter", "*", "--job", "Dry", "--packages", Path.GetTempPath() },
                ConsoleLogger.Default, config);
            Assert.True(success);
            Assert.NotNull(parsed);
            BenchmarkRunInfo benchmarks = BenchmarkConverter.TypeToBenchmarks(typeof(Probe), parsed);

            Assert.Empty(package.ValidateAsync(
                new ValidationParameters(benchmarks.BenchmarksCases, benchmarks.Config)).ToBlockingEnumerable());
        }

        public class Probe
        {
            [Benchmark]
            public byte[] Allocate() => new byte[1000];
        }

        private sealed class TestEnvironment : IEnvironment
        {
            private readonly Dictionary<string, string?> _values;

            public TestEnvironment(string? version = null, string? rid = null, string? smoke = null)
            {
                _values = new Dictionary<string, string?>
                {
                    [RuntimePackageConfig.VersionVariable] = version,
                    [RuntimePackageConfig.RuntimeIdentifierVariable] = rid,
                    [RuntimePackageConfig.SmokeVariable] = smoke
                };
            }

            public string GetEnvironmentVariable(string variable)
                => _values.GetValueOrDefault(variable)!;

            public IDictionary GetEnvironmentVariables() => _values;
        }
    }
}
