FROM mcr.microsoft.com/dotnet/sdk:10.0 AS build
WORKDIR /src
COPY StayVC.csproj ./
RUN dotnet restore
COPY Program.cs ./
RUN dotnet publish -c Release -o /app/out --no-restore

FROM mcr.microsoft.com/dotnet/runtime:10.0
RUN apt-get update && apt-get install -y --no-install-recommends \
    libopus0 libsodium23 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=build /app/out ./
ENV DOTNET_EnableDiagnostics=0
ENV DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1
CMD ["dotnet", "StayVC.dll"]
