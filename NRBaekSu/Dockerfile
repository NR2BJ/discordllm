FROM mcr.microsoft.com/dotnet/sdk:10.0 AS build
RUN apt-get update && apt-get install -y --no-install-recommends \
    libopus0 libsodium23 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY . ./
RUN dotnet restore ./NRBaekSu.csproj
ENV DOTNET_EnableDiagnostics=0
ENV DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=0
CMD ["dotnet", "run", "--project", "NRBaekSu.csproj", "-c", "Release", "--no-restore"]
