using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using System.Threading.Tasks;
using ExifLib;

namespace OrderMyPhotos
{
    class Program
    {
        static void Main(string[] args)
        {
            if (!args.Any()) throw new ArgumentNullException("args", "Please pass photos directory.");
            var dir = new DirectoryInfo(args[0]);
            if (!dir.Exists) throw new ArgumentNullException("args", "Please pass correct photos directory.");

            OrganizePhotosInDirectory(dir);
        }

        private static void OrganizePhotosInDirectory(DirectoryInfo dir, bool recursive = true)
        {
            foreach (var file in dir.EnumerateFiles("*.jpg", recursive ? SearchOption.AllDirectories : SearchOption.TopDirectoryOnly))
            {
                var time = GetTakenTime(file);
                var dateDir = GetAndCreateDateDirectory(dir, time);
                file.MoveTo(GenerateDestFileName(dateDir, time, file));
            }
        }

        private static DirectoryInfo GetAndCreateDateDirectory(DirectoryInfo dir, DateTime time)
        {
            var dateDir = new DirectoryInfo(Path.Combine(dir.FullName, time.ToString("yyyy-MM-dd")));
            if (!dateDir.Exists) dateDir.Create();
            return dateDir;
        }

        private static string GenerateDestFileName(DirectoryInfo dateDir, DateTime time, FileInfo file, int count = 0)
        {
            var name = Path.Combine(dateDir.FullName, GenerateFileNameFromTime(time, count));
            if (file.FullName == name) return name;
            if (File.Exists(name))
                name = GenerateDestFileName(dateDir, time, file, ++count);
            return name;
        }

        private static string GenerateFileNameFromTime(DateTime time, int count)
        {
            return time.ToString("yyyy-MM-dd HH.mm.ss") + (count == 0 ? "" : " (" + count + ")") + ".jpg";
        }

        private static DateTime GetTakenTime(FileInfo file)
        {
            try
            {
                DateTime time;

                using (var exif = new ExifReader(file.FullName))
                {
                    var success = exif.GetTagValue(ExifTags.DateTimeDigitized, out time);
                    if (!success)
                        time = file.LastWriteTime;
                }
                return time;
            }
            catch (ExifLibException ex)
            {
                return file.LastWriteTime;
            }
        }
    }
}
