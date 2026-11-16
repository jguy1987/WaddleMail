Name:           waddlemail
Version:        1.0.0
Release:        1%{?dist}
Summary:        Linux mail client for Microsoft 365

License:        MIT
URL:            https://github.com/jmish1987/WaddleMail
Source0:        WaddleMail-%{version}.tar.gz
BuildArch:      noarch

BuildRequires:  python3-devel
BuildRequires:  pyproject-rpm-macros
BuildRequires:  desktop-file-utils
Requires:       hicolor-icon-theme

%description
WaddleMail is a mail, calendar, contacts and tasks client for Microsoft 365 and
Outlook.com built on Microsoft Graph. It supports several accounts and shared
mailboxes, favorites, folder management, an outbox, and desktop notifications.

%prep
%autosetup -n WaddleMail-%{version}

%generate_buildrequires
%pyproject_buildrequires

%build
%pyproject_wheel

%install
%pyproject_install
%pyproject_save_files waddlemail
packaging/install-data.sh %{buildroot}

%check
desktop-file-validate %{buildroot}%{_datadir}/applications/waddlemail.desktop
%pyproject_check_import -e 'waddlemail.ui*' -e 'waddlemail.__main__'

%files -f %{pyproject_files}
%license LICENSE
%doc README.md
%{_bindir}/waddlemail
%{_datadir}/applications/waddlemail.desktop
%{_datadir}/icons/hicolor/*/apps/waddlemail*

%changelog
* Mon Nov 16 2026 jmish <jmish1987@gmail.com> - 1.0.0-1
- First stable release
