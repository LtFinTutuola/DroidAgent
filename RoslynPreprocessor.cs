using System;
using System.IO;
using System.Linq;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;

namespace RoslynPreprocessor
{
    class Program
    {
        static void Main(string[] args)
        {
            var parseOptions = new CSharpParseOptions(
                preprocessorSymbols: new[] { "ANDROID", "MonoDroid" },
                languageVersion: LanguageVersion.Latest,
                documentationMode: DocumentationMode.Parse
            );
            var rewriter = new CleanRewriter();

            Console.InputEncoding = System.Text.Encoding.UTF8;
            Console.OutputEncoding = System.Text.Encoding.UTF8;

            // Sentinel used to mark end of input/output blocks
            const string Sentinel = "===END_OF_CODE===";

            while (true)
            {
                string line;
                var codeBuffer = new System.Text.StringBuilder();
                
                // Read until sentinel or stream end
                while ((line = Console.ReadLine()) != null)
                {
                    if (line == Sentinel) break;
                    codeBuffer.AppendLine(line);
                }

                if (line == null && codeBuffer.Length == 0) break; // Finished

                string code = codeBuffer.ToString();
                if (string.IsNullOrWhiteSpace(code)) 
                {
                    Console.WriteLine(Sentinel);
                    continue;
                }

                try
                {
                    var tree = CSharpSyntaxTree.ParseText(code, parseOptions);
                    var root = tree.GetRoot();
                    
                    var triviaToRemove = root.DescendantTrivia().Where(t => 
                        t.IsKind(SyntaxKind.DisabledTextTrivia) ||
                        t.IsKind(SyntaxKind.IfDirectiveTrivia) ||
                        t.IsKind(SyntaxKind.ElifDirectiveTrivia) ||
                        t.IsKind(SyntaxKind.ElseDirectiveTrivia) ||
                        t.IsKind(SyntaxKind.EndIfDirectiveTrivia) ||
                        t.IsKind(SyntaxKind.RegionDirectiveTrivia) ||
                        t.IsKind(SyntaxKind.EndRegionDirectiveTrivia)
                    );
                    
                    root = root.ReplaceTrivia(triviaToRemove, (o, r) => default(SyntaxTrivia));
                    root = rewriter.Visit(root);
                    
                    Console.WriteLine(root.ToFullString());
                }
                catch (Exception ex)
                {
                    Console.Error.WriteLine($"Error processing code: {ex.Message}");
                }
                finally
                {
                    // Always send sentinel back so Python knows we are done with this file
                    Console.WriteLine(Sentinel);
                }
            }
        }
    }
    
    class CleanRewriter : CSharpSyntaxRewriter
    {
        public override SyntaxNode VisitUsingDirective(UsingDirectiveSyntax node)
        {
            var name = node.Name.ToString();
            // Drop external namespaces. Keep System, Microsoft, and TCPOS.
            if (name.StartsWith("System") || name.StartsWith("Microsoft") || name.StartsWith("TCPOS"))
            {
                return base.VisitUsingDirective(node);
            }
            return null; // Remove this node completely
        }
    }
}
