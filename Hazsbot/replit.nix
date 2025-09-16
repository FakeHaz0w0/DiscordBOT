  { pkgs }: {
  deps = [
    pkgs.python311
    pkgs.python311Packages.pip
  ];

  env = {
    PYTHONNOUSERSITE = "1";
  };
}
