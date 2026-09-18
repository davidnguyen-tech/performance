// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.
// See the LICENSE file in the project root for more information.

using System;
using System.Collections.Generic;
using System.Linq;
using System.Runtime.Versioning;
using System.Text.RegularExpressions;
using BenchmarkDotNet.Jobs;
using BenchmarkDotNet.Toolchains.CsProj;
using BenchmarkDotNet.Toolchains.DotNetCli;
using BenchmarkDotNet.Validators;
using Reporting;

namespace BenchmarkDotNet.Extensions
{
    public sealed class RuntimePackageConfig : IValidator
    {
        public const string VersionVariable = "PERFLAB_RUNTIME_PACKAGE_VERSION";
        public const string RuntimeIdentifierVariable = "PERFLAB_RUNTIME_PACKAGE_RID";
        public const string SmokeVariable = "PERFLAB_RUNTIME_PACKAGE_SMOKE";

        private readonly Argument[] _arguments;

        public string PackageVersion { get; }
        public string RuntimeIdentifier { get; }
        public string TargetFrameworkMoniker { get; }
        public bool IsSmoke { get; }
        public bool TreatsWarningsAsErrors => true;

        public RuntimePackageConfig(
            string? packageVersion,
            string? runtimeIdentifier,
            string? targetFrameworkMoniker,
            bool isSmoke = false)
        {
            if (string.IsNullOrEmpty(packageVersion) ||
                !Regex.IsMatch(packageVersion, @"\A[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?\z", RegexOptions.CultureInvariant))
            {
                throw new ArgumentException($"{VersionVariable} must specify one exact NuGet version, without wildcards, ranges, or build metadata.");
            }

            if (string.IsNullOrEmpty(runtimeIdentifier) ||
                !Regex.IsMatch(runtimeIdentifier, @"\A(?:linux(?:-musl)?|osx|win)-(?:x64|x86|arm64|arm)\z", RegexOptions.CultureInvariant))
            {
                throw new ArgumentException($"{RuntimeIdentifierVariable} must specify a supported portable runtime identifier.");
            }

            if (string.IsNullOrEmpty(targetFrameworkMoniker) ||
                !Regex.IsMatch(targetFrameworkMoniker, @"\Anet(?:[5-9]|[1-9][0-9]+)\.0\z", RegexOptions.CultureInvariant))
            {
                throw new ArgumentException("Runtime package benchmarks require one .NET target framework.");
            }

            PackageVersion = packageVersion;
            RuntimeIdentifier = runtimeIdentifier;
            TargetFrameworkMoniker = targetFrameworkMoniker;
            IsSmoke = isSmoke;
            _arguments = new Argument[]
            {
                new MsBuildProperty("RuntimeFrameworkVersion", PackageVersion),
                new MsBuildProperty("RuntimeIdentifier", RuntimeIdentifier),
                new MsBuildProperty("SelfContained", "true"),
                new MsBuildProperty("UseAppHost", "true")
            };
        }

        public static RuntimePackageConfig? FromEnvironment(IEnvironment environment, string? targetFrameworkName)
        {
            string? version = environment.GetEnvironmentVariable(VersionVariable);
            string? rid = environment.GetEnvironmentVariable(RuntimeIdentifierVariable);
            string? smokeValue = environment.GetEnvironmentVariable(SmokeVariable);
            if (string.IsNullOrEmpty(version) && string.IsNullOrEmpty(rid) && string.IsNullOrEmpty(smokeValue))
            {
                return null;
            }

            bool isSmoke = smokeValue switch
            {
                null or "" or "0" => false,
                "1" => true,
                _ => throw new ArgumentException($"{SmokeVariable} must be unset, 0, or 1.")
            };

            if (string.IsNullOrEmpty(targetFrameworkName))
            {
                throw new ArgumentException("Cannot determine the benchmark application's target framework.");
            }

            var framework = new FrameworkName(targetFrameworkName);
            if (framework.Identifier != ".NETCoreApp")
            {
                throw new ArgumentException("Runtime package benchmarks require a .NET application.");
            }

            return new RuntimePackageConfig(
                version,
                rid,
                $"net{framework.Version.Major}.{framework.Version.Minor}",
                isSmoke);
        }

        public Job ApplyTo(Job job)
        {
            // BDN generates another executable. Configure both its project and the restore/build
            // arguments, including the reference-gathering build, rather than only the controller.
            var settings = new NetCoreAppSettings(
                TargetFrameworkMoniker,
                PackageVersion,
                $"Runtime package {PackageVersion}");

            return job
                .WithId($"RuntimePackage-{PackageVersion}")
                .WithToolchain(CsProjCoreToolchain.From(settings))
                .WithGcServer(false)
                .WithGcConcurrent(true)
                .WithArguments(_arguments);
        }

        public IAsyncEnumerable<ValidationError> ValidateAsync(ValidationParameters validationParameters)
            => GetErrors(validationParameters).ToAsyncEnumerable();

        private IEnumerable<ValidationError> GetErrors(ValidationParameters parameters)
        {
            Job[] jobs = parameters.Benchmarks.Select(benchmark => benchmark.Job).Distinct().ToArray();
            if (jobs.Length != 1)
            {
                yield return new ValidationError(true, "Runtime package mode requires exactly one benchmark job; do not combine it with a runtime comparison.");
            }

            foreach (Job job in jobs)
            {
                if (job.Infrastructure.Toolchain is not CsProjCoreToolchain toolchain ||
                    toolchain.Generator is not CsProjGenerator generator ||
                    generator.TargetFrameworkMoniker != TargetFrameworkMoniker ||
                    job.Environment.Gc.Server != false ||
                    job.Environment.Gc.Concurrent != true ||
                    job.Infrastructure.Arguments is not { } arguments ||
                    !arguments.SequenceEqual(_arguments))
                {
                    yield return new ValidationError(
                        true,
                        "Runtime package settings were replaced. Do not combine package mode with another toolchain, target framework, or MSBuild runtime override.");
                }
            }
        }
    }
}
